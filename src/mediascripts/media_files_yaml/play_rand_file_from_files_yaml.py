import argparse
import random
import subprocess

from mediascan.media_files_yaml_file_loader import load_media_files_yaml_file

"""
Reads yaml file output by mediascan and plays a random file
"""


def main():
    parser = argparse.ArgumentParser(
        description="Plays a random file from a media files YAML file."
    )
    parser.add_argument("player_cmd", help="Command used to play the file")
    parser.add_argument("files_yaml_file", help="Path to the files YAML file")
    args = parser.parse_args()
    files = load_media_files_yaml_file(args.files_yaml_file)
    file = random.choice(files.files)
    subprocess.run([args.player_cmd, file.path], check=False)


if __name__ == "__main__":
    main()
