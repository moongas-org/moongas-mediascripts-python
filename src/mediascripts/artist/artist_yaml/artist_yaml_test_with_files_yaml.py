import argparse
import os
from pathlib import Path

from mediascan.artist_yaml_file_validator import validate_artist_yaml_file
from mediascan.media_files_yaml_file_loader import load_media_files_yaml_file

"""
Tests whether media library artist dirs have valid artists.yml file
(TODO: Integrate this into mediatest)

Note: Some of these tests should be moved to mediatest, once it has been refactored 
to test mediascan database instead of mediascan files.yml file

The scope of this script should be reduced to only test that the artist.yml files are
present and syntactically valid.

artist.yml file example:

artist_data:
  artist_names:
  - Parcels
  city: Byron Bay
  country_code: AU
  region_code: AU-NSW
  language_codes:
  - en


"""


EXCLUDE_DIRS = ["Various Artists"]


def excluded(path: Path) -> bool:
    for d in EXCLUDE_DIRS:
        if d in str(path):
            return True
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Tests whether media library artist dirs have valid artist.yml files."
    )
    parser.add_argument(
        "--files-yaml-path",
        default="../../out/files.yml",
        help="Path to the files YAML file (default: %(default)s)",
    )
    parser.add_argument(
        "--source-path",
        dest="source_paths",
        action="append",
        help="Media library source path to scan for artist.yml files. "
        "Repeat for multiple paths (default: /data/Music and /data/MusicOther)",
    )
    args = parser.parse_args()

    files_yaml_path = args.files_yaml_path

    # TODO: Read source paths from config
    artist_yaml_paths: list[str] = []
    source_paths = (
        args.source_paths if args.source_paths else ["/data/Music", "/data/MusicOther"]
    )
    for source_path in source_paths:
        for root, _, files in os.walk(source_path):
            for f in files:
                if f == "artist.yml":
                    full_path = os.path.join(root, f)
                    absolute_path = os.path.abspath(full_path)
                    artist_yaml_paths.append(str(absolute_path))
                    print(f"found artist.yml: absolute_path={absolute_path}")
    artist_yaml_paths = list(set(artist_yaml_paths))

    files = load_media_files_yaml_file(files_yaml_path)
    artist_paths: dict[str, Path] = {}
    for file in files.files:
        if file.artist not in artist_paths:
            artist_paths[file.artist] = Path(file.path).parent.parent
    print(f"loaded {len(artist_paths)} artist paths from mediafiles yaml")
    artists_missing: list[str] = []
    exceptions: list[tuple[Path, Exception]] = []
    for artist, artist_path in artist_paths.items():
        if excluded(artist_path):
            continue

        validate_artist_yaml_file(
            artist,
            artist_path,
            artist_yaml_paths,
            artists_missing,
            exceptions,
        )
    exist_count = len(artist_paths) - len(artists_missing)
    print(
        f"Found artist.yml for {exist_count} out of {len(artist_paths)} artist folders"
    )
    print(f"Missing/Invalid count: {len(artists_missing)}")
    if len(artists_missing) > 0:
        print("Writing artists missing artist.yml list to artists_missing.txt")
        with open("artists_missing.txt", "w") as f:
            for artist in sorted(artists_missing):
                f.write(artist + os.linesep)
    if len(exceptions) > 0:
        print("Exceptions:")
        for artist_yaml_path, ex in exceptions:
            print(artist_yaml_path, ex)

    # artist_yaml_paths should now be empty
    # if it's not empty, that means there is an artist.yml file on the filesystem that is not
    # in the artist path of any artist, so it's probably in the wrong directory
    if len(artist_yaml_paths) > 0:
        print("Warnings:")
        print(
            "Validation skipped for the following artist.yml files which are not in any artist path:"
        )
        for p in artist_yaml_paths:
            print(p)


if __name__ == "__main__":
    main()
