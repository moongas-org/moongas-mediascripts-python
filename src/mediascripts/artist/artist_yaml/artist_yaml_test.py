"""Validate artist YAML files against the artist paths in mediascan.db."""

import argparse
import os
from pathlib import Path

from mediascan.types.artist.artist_yaml_file_validator import validate_artist_yaml_file
from mediascripts.artist.artist_yaml.artist_yaml_common import (
    excluded,
    load_artist_paths_from_database,
    print_artist_yaml_summary,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tests whether media library artist directories have valid artist.yml files."
    )
    parser.add_argument(
        "--source-path",
        dest="source_paths",
        action="append",
        help="Media library source path to scan for artist.yml files. "
        "Repeat for multiple paths (default: /data/Music and /data/MusicOther)",
    )
    args = parser.parse_args()

    artist_yaml_paths: list[str] = []
    source_paths = args.source_paths if args.source_paths else ["/data/Music", "/data/MusicOther"]
    for source_path in source_paths:
        for root, _, files in os.walk(source_path):
            for filename in files:
                if filename == "artist.yml":
                    artist_yaml_path = Path(root, filename).resolve()
                    artist_yaml_paths.append(str(artist_yaml_path))
                    print(f"found artist.yml: absolute_path={artist_yaml_path}")
    artist_yaml_paths = list(set(artist_yaml_paths))

    artist_paths = load_artist_paths_from_database(parser)
    artists_missing: list[str] = []
    exceptions: list[tuple[Path, Exception]] = []
    for artist, artist_path in artist_paths.items():
        if excluded(artist_path):
            continue
        validate_artist_yaml_file(
            artist,
            artist_path.resolve(),
            artist_yaml_paths,
            artists_missing,
            exceptions,
        )

    print_artist_yaml_summary(len(artist_paths), len(artists_missing), "Missing/Invalid count")
    if artists_missing:
        print("Writing artists missing artist.yml list to artists_missing.txt")
        with open("artists_missing.txt", "w", encoding="utf-8") as file:
            for artist in sorted(artists_missing):
                file.write(artist + os.linesep)
    if exceptions:
        print("Exceptions:")
        for artist_yaml_path, exception in exceptions:
            print(artist_yaml_path, exception)

    if artist_yaml_paths:
        print("Warnings:")
        print("Validation skipped for the following artist.yml files which are not in any artist path:")
        for path in artist_yaml_paths:
            print(path)


if __name__ == "__main__":
    main()
