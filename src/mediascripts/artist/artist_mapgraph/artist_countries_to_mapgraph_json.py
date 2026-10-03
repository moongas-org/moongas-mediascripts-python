"""
Reads artists countries data from mediascan database
Outputs mapgraph json data file with artist and track count for each country
"""

import argparse
import json
import shutil
import subprocess
from typing import Any, Callable, TypedDict

import pandas as pd
from sqlalchemy import create_engine


class MapGraphData(TypedDict):
    nodeIdSource: str
    urlBase: str
    urlValueSource: str
    metadata: list[dict[str, str]]
    data: list[dict[str, str | int]]


def convert_country_code_to_country_code(input: str) -> str:
    """Converts alpha-2 countryCode to alpha-2 countryCode (i.e. no-op)"""
    return input


def convert_state_code_to_region_code(input: str):
    """Converts a US state abbreviation to an ISO regionCode (e.g. AL -> US-AL)"""
    return f"US-{input}"


def get_country_code_from_tuple(tuple: Any) -> str:
    return str(tuple.countrycode)  # type: ignore


def get_region_code_from_tuple(tuple: Any) -> str:
    return str(tuple.regioncode)  # type: ignore


def artist_counts_mapgraph_dataset(
    output_filename: str,
    mapgraph_datasets_path: str,
    codes_json_fpath: str,
    code_convert_func: Callable[[str], str],
    get_code_from_tuple_func: Callable[[Any], str],
    url_param: str,
):

    codes_list = []
    with open(codes_json_fpath, "r", encoding="utf-8") as file:
        codes_list = json.load(file)
        codes_list = sorted(codes_list, key=lambda x: x["alpha_2_code"])

    db_path = "sqlite:///../../out/mediascan.db"
    engine = create_engine(db_path)
    with engine.connect() as conn:
        artists = pd.read_sql_query("SELECT * FROM artist", conn)
        files = pd.read_sql_query(
            "SELECT * FROM mediafile LEFT JOIN artist ON mediafile.artistpath = artist.path",
            conn,
        )

        # Get artist counts
        artist_counts: dict[str, int] = {}
        for a in artists.itertuples():
            code = get_code_from_tuple_func(a)
            if code in artist_counts:
                artist_counts[code] += 1
            else:
                artist_counts[code] = 1

        # Get file counts
        file_counts: dict[str, int] = {}
        for f in files.itertuples():
            code = get_code_from_tuple_func(f)
            if code in file_counts:
                file_counts[code] += 1
            else:
                file_counts[code] = 1

        data: MapGraphData = {
            "nodeIdSource": "id",
            "urlBase": f"/mediaserver/artists?sort=count&{url_param}=",
            "urlValueSource": "alpha_code",
            "metadata": [],
            "data": [],
        }
        data["metadata"].append(
            {"Data_Item": "id", "Item_Description": "numeric code (mapgraph code)"}
        )
        data["metadata"].append(
            {"Data_Item": "alpha_code", "Item_Description": "mediascan code"}
        )
        data["metadata"].append(
            {"Data_Item": "ARTIST.COUNT", "Item_Description": "Artist Count"}
        )
        data["metadata"].append(
            {"Data_Item": "TRACK.COUNT", "Item_Description": "Track Count"}
        )

        for code in codes_list:
            alpha_code = code_convert_func(code["alpha_2_code"])
            numeric_code = int(code["numeric_code"])
            artist_count = 0
            file_count = 0
            if alpha_code in artist_counts:
                artist_count = artist_counts[alpha_code]
                file_count = file_counts[alpha_code]
            data["data"].append(
                {
                    "id": numeric_code,
                    "alpha_code": alpha_code,
                    "ARTIST.COUNT": artist_count,
                    "TRACK.COUNT": file_count,
                }
            )

        # write json to file
        with open(output_filename, "w") as of:
            json.dump(data, of, indent=4)
        # minify json
        output_filename_min = output_filename.replace(".json", ".min.json")
        with open(output_filename_min, "w", encoding="utf-8") as minified_file:
            subprocess.run(
                ["jq", "-c", ".", output_filename],
                stdout=minified_file,
                check=True,
            )
        # copy both json files to mapgraph datasets
        shutil.copy(output_filename, mapgraph_datasets_path)
        shutil.copy(output_filename_min, mapgraph_datasets_path)


def main():
    parser = argparse.ArgumentParser(
        description="Outputs mapgraph JSON data files with artist counts per country and US state."
    )
    parser.add_argument(
        "--mapgraph-datasets-path",
        default="/home/user/Git/moongas-org/moongas.org.local/assets/projects/mapgraph/datasets",
        help="Base path of the mapgraph datasets directory (default: %(default)s)",
    )
    args = parser.parse_args()
    artist_counts_mapgraph_dataset(
        "mediaserver-artist-country-counts.json",
        f"{args.mapgraph_datasets_path}/world/",
        "country_codes.json",
        convert_country_code_to_country_code,
        get_country_code_from_tuple,
        "countryCode",
    )
    artist_counts_mapgraph_dataset(
        "mediaserver-artist-us-state-counts.json",
        f"{args.mapgraph_datasets_path}/us-states/",
        "state_codes.json",
        convert_state_code_to_region_code,
        get_region_code_from_tuple,
        "regionCode",
    )


if __name__ == "__main__":
    main()
