import argparse
import json
import os
from pathlib import Path
from typing import Set

from mediascan.types.artist.artist_yaml_file_loader import load_artist_yaml_file


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
        "--artists-path",
        "--artists-yaml-path",
        dest="artists_path",
        default="../../moongas-collection-demo/data/Music",
        help="Artist directory or one artist.yml file (default: %(default)s)",
    )
    args = parser.parse_args()
    regions_code_name_map_json_path = args.regions_code_name_map_json_path
    artists_path = Path(args.artists_path)
    if artists_path.is_dir():
        artist_yaml_paths = [
            Path(root) / filename
            for root, _, filenames in os.walk(artists_path, followlinks=True)
            for filename in filenames
            if filename == "artist.yml"
        ]
    else:
        artist_yaml_paths = [artists_path]

    with open(regions_code_name_map_json_path) as f:
        missing: Set[str] = set()
        code_name_map = json.loads(f.read())
        for artist_yaml_path in artist_yaml_paths:
            artist = load_artist_yaml_file(artist_yaml_path)
            region_code = artist.artist_data.region_code
            if region_code not in code_name_map:
                missing.add(region_code)
        if len(missing):
            for code in missing:
                print(code)
        else:
            print("No codes missing")


if __name__ == "__main__":
    main()
