import argparse
from pathlib import Path

from mediascan.media_file_data import MediaFileData
from mediascan.media_files_yaml_file_loader import load_media_files_yaml_file


def list_artists(files: list[MediaFileData]):
    artists: set[str] = set()
    for f in files:
        artists.add(f.artist)
    for a in artists:
        print(a)


def main():
    parser = argparse.ArgumentParser(
        description="Lists all artists found in a media files YAML file."
    )
    parser.add_argument("files_yaml_file", help="Path to the files YAML file")
    args = parser.parse_args()
    files_yaml_file = load_media_files_yaml_file(Path(args.files_yaml_file))
    files = files_yaml_file.files
    list_artists(files)


if __name__ == "__main__":
    main()
