import argparse
import os
from pathlib import Path
from typing import Mapping, Union, cast

import pandas as pd

from mediascan.artist_data import ArtistData, ArtistMember, Date
from mediascan.artist_yaml_file import ArtistYamlFile

"""
Generates artist.yml files from artist data CSV files
(CSV output by Google Gemini, in chunks of 25-150 artists, which 
is manageable given that I only have approx. 2500 artists total)

artist.yml file example:

artistData:
  artistNames:
  - Parcels
  city: Byron Bay
  countryCode: AU
  regionCode: AU-NSW
  languageCodes:
  - en

country_code = Country (ISO 3166-1) alpha-2 e.g. "GB" or "PR"
region_code = Country Region/Subdivision (ISO 3166-2) e.g. "GB-NIR" or "US-PR"
language_codes = Language (ISO 639-1)

"""

ArtistDataPrimitive = dict[str, Union[str, list[str]]]


def parse_date(value: object, field_name: str) -> Date | None:
    """Build a Date from a mapping containing required y and optional m/d."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping with a y value")
    date_values = cast(Mapping[str, object], value)
    if "y" not in date_values:
        raise ValueError(f"{field_name} is missing required y value")

    return Date(
        y=int(str(date_values["y"])),
        m=(int(str(date_values["m"])) if date_values.get("m") is not None else None),
        d=(int(str(date_values["d"])) if date_values.get("d") is not None else None),
    )


def get_path_depth(path: str):
    return len(path.strip(os.path.sep).split(os.path.sep))


def write_yaml_file(filepath: Path, artist_data: ArtistDataPrimitive):
    dob = parse_date(artist_data["dob"], "dob")
    if dob is None:
        raise ValueError("dob is required")
    dod = parse_date(artist_data.get("dod"), "dod")
    ad = ArtistData(
        list(artist_data["artist_names"]),
        str(artist_data["city"]),
        str(artist_data["country_code"]),
        str(artist_data["region_code"]),
        list(artist_data["language_codes"]),
        dob,
        cast(list[ArtistMember], artist_data["members"]),
        dod,
    )
    adf = ArtistYamlFile(ad)
    try:
        yaml_str: str = adf.to_yaml(allow_unicode=True)
        # print(yaml_str)
        with open(filepath, "w") as f:
            f.write(yaml_str)
        print(f"Successfully wrote data to {filepath}")
    except IOError as e:
        print(f"Error writing to file: {e}")


def get_artist(row: pd.Series) -> str:
    return row["Artist"]


def get_city(row: pd.Series) -> str:
    return row["City"]


def get_country(row: pd.Series):
    return row["Country (ISO 3166-1)"].upper()


def get_region(row: pd.Series):
    return row["Region (ISO 3166-2)"].upper()


def get_language(row: pd.Series):
    return row["Language (ISO 639-1)"].lower()


def escape_artist_name(name: str) -> str:
    """
    Escapes an artist name so that it matches mediax artist
    folder name escaping conventions
    """
    ret = name.strip(">").strip("<")
    ret = ret.replace("$", "S")
    ret = ret.replace("&", "and")
    ret = ret.replace("!", "_")
    ret = ret.replace("?", "_")
    ret = ret.replace('"', "_")
    ret = ret.replace(".", "_")
    return ret


def find_artist_from_dir_name(dir_name: str, artist_names: list[str]) -> str:
    for a in artist_names:
        if dir_name.lower().replace("the ", "") == escape_artist_name(
            a
        ).lower().replace("the ", ""):
            return a
    return ""


def read_csv_file(csv_filepath: Path):
    # csv_filepath = "artist_data/artist_data.csv"
    artists_data: dict[str, ArtistDataPrimitive] = {}
    df = pd.read_csv(csv_filepath)
    for _, row in df.iterrows():
        artist = get_artist(row).strip()
        artist_data: ArtistDataPrimitive = {
            # artist names, plural. use case:
            # The Ohsees, The Oh Sees, Oh Sees, Osees
            "artist_names": [artist],
            "city": get_city(row),
            "country_code": get_country(row),
            "region_code": get_region(row),
            "language_codes": [get_language(row)],
        }
        artists_data[artist] = artist_data

    # test artist yaml file generation here
    # write_yaml_file("artist.yml", {"artist_data": artists_data["Parcels"]})
    # return

    SOURCE_PATHS = [
        "/home/user/Downloads/data/Music",
        "/home/user/Downloads/data/MusicOther",
    ]
    overwrite_existing_artist_yaml = False
    file_write_count = 0
    file_overwrite_count = 0
    artists_found: list[str] = []
    for source_path in SOURCE_PATHS:
        base_depth = get_path_depth(source_path)
        for root, dirs, _ in os.walk(source_path, topdown=False):
            for d_name in dirs:
                fullpath = os.path.join(root, d_name)
                current_depth = get_path_depth(fullpath)
                # 1 = artist dir
                # 2 = album [year] dir
                if current_depth - base_depth == 1:
                    d_abs_path = Path(source_path).joinpath(d_name)
                    artist_yaml_dst_path = Path(d_abs_path).joinpath("artist.yml")
                    artist_names = list(artists_data.keys())
                    found_artist_name = find_artist_from_dir_name(d_name, artist_names)
                    if found_artist_name != "":
                        artists_found.append(found_artist_name)
                        artist_data = artists_data[found_artist_name]
                        if (
                            overwrite_existing_artist_yaml
                            or not artist_yaml_dst_path.exists()
                        ):
                            file_write_count += 1
                            if artist_yaml_dst_path.exists():
                                file_overwrite_count += 1
                            write_yaml_file(artist_yaml_dst_path, artist_data)
                    else:
                        print(f"Could not find artist from directory '{d_name}'")

    print(f"overwrite_existing_artist_yaml={overwrite_existing_artist_yaml}")
    print(f"files written (including overwrites): {file_write_count}")
    print(f"files overwritten: {file_overwrite_count}")
    artists = artists_data.keys()
    artists_not_found = set(artists) - set(artists_found)
    print("")
    print("Warning: CSV artists not found in filesystem artist folders:")
    print("")
    for artist in artists_not_found:
        print(f"'{artist}' '{escape_artist_name(artist)}'")
    return file_write_count, file_overwrite_count


def main():
    parser = argparse.ArgumentParser(
        description="Generates artist.yml files from artist data CSV files."
    )
    parser.add_argument(
        "csv_path",
        help="Path to a folder containing artist data CSV files, or a single CSV file",
    )
    args = parser.parse_args()
    csv_path = args.csv_path

    tot_file_write_count = 0
    tot_file_overwrite_count = 0

    csv_filepaths: list[Path] = []
    if Path(csv_path).is_dir():
        # process all csv files in specified dir
        for csv_filename in os.listdir(csv_path):
            csv_filepath = Path(csv_path).joinpath(csv_filename)
            csv_filepaths.append(csv_filepath)
    else:
        # process the specified csv file
        csv_filepaths.append(Path(csv_path))

    for csv_filepath in csv_filepaths:
        try:
            file_write_count, file_overwrite_count = read_csv_file(csv_filepath)
            tot_file_write_count += file_write_count
            tot_file_overwrite_count += file_overwrite_count
        except Exception as ex:
            print(f"Exception reading file {csv_filepath}:")
            print(ex)

    print("totals for all CSV files:")
    print(f"total files written (including overwrites): {tot_file_write_count}")
    print(f"total files overwritten: {tot_file_overwrite_count}")


if __name__ == "__main__":
    main()
