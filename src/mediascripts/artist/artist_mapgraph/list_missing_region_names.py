import argparse
import json
from pathlib import Path
from typing import Set

from mediascan.artists_yaml_file_loader import load_artists_yaml_file


def main():
    parser = argparse.ArgumentParser(
        description="Lists region codes from artists.yml that are missing from the region code/name map."
    )
    parser.add_argument(
        "--regions-code-name-map-json-path",
        default="../../../mediaserver/app/static/json_data/region_code_name_map.json",
        help="Path to the region code/name map JSON file (default: %(default)s)",
    )
    parser.add_argument(
        "--artists-yaml-path",
        default="../../out/artists.yml",
        help="Path to the artists YAML file (default: %(default)s)",
    )
    args = parser.parse_args()
    regions_code_name_map_json_path = args.regions_code_name_map_json_path
    artists_yaml_path = Path(args.artists_yaml_path)
    artists = load_artists_yaml_file(artists_yaml_path)

    with open(regions_code_name_map_json_path) as f:
        missing: Set[str] = set()
        code_name_map = json.loads(f.read())
        for a in artists.artists:
            if a.artist_data.region_code not in code_name_map:
                missing.add(a.artist_data.region_code)
        if len(missing):
            for code in missing:
                print(code)
        else:
            print("No codes missing")


if __name__ == "__main__":
    main()
