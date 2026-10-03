"""
Batch modify artist.yml files to make changes to the yaml format
"""

import argparse
from pathlib import Path

from mediascan.artist_yaml_file_loader import load_artist_yaml_file
from mediascan.media_files_yaml_file_loader import load_media_files_yaml_file

"""
Converts artist.yml files from one format to another
(used to make changes to the format)

artist.yml file example:


Old format:

artist_data:
  artist_names:
  - Parcels
  city: Byron Bay
  country_code: AU
  region_code: AU-NSW
  language_codes:
  - en

New Format:

artistData:
  artistNames:
  - Parcels
  city: Byron Bay
  countryCode: AU
  regionCode: AU-NSW
  languageCodes:
  - en

"""

EXCLUDE_DIRS = ["Various Artists"]


REFORMAT_ALL = False


def excluded(path: Path):
    for d in EXCLUDE_DIRS:
        if d in str(path):
            return True
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Batch modifies artist.yml files to make changes to the yaml format."
    )
    parser.add_argument(
        "--files-yaml-path",
        default="../../out/files.yml",
        help="Path to the files YAML file (default: %(default)s)",
    )
    parser.add_argument(
        "--reformat-all",
        action="store_true",
        help="Reformat all artist.yml files, not just those matching the built-in rules",
    )
    args = parser.parse_args()
    files_yaml_path = Path(args.files_yaml_path)
    files = load_media_files_yaml_file(files_yaml_path)
    artist_paths: dict[str, Path] = {}
    for file in files.files:
        if file.artist not in artist_paths:
            artist_paths[file.artist] = Path(file.path).parent.parent
    print(f"loaded {len(artist_paths)} artist paths from mediafiles yaml")
    artists_missing: list[str] = []
    artists_updated: list[Path] = []
    exceptions: list[tuple[Path, Exception]] = []
    for artist, artist_path in artist_paths.items():
        if excluded(artist_path):
            continue

        artist_yaml_path = Path(artist_path).joinpath("artist.yml")
        if not artist_yaml_path.exists():
            print(f"{artist_path} missing artist.yml")
            artists_missing.append(artist)
        else:
            # convert yaml (if applicable)
            try:
                adf = load_artist_yaml_file(artist_yaml_path)
                reformat_applicable = REFORMAT_ALL or args.reformat_all

                if (
                    adf.artist_data.country_code == "GB"
                    and adf.artist_data.city == "Birmingham"
                    and adf.artist_data.region_code != "GB-WMD"
                ):
                    reformat_applicable = True
                    adf.artist_data.region_code = "GB-WMD"

                if (
                    adf.artist_data.country_code == "GB"
                    and adf.artist_data.city == "Manchester"
                    and adf.artist_data.region_code != "GB-NWK"
                ):
                    reformat_applicable = True
                    adf.artist_data.region_code = "GB-NWK"

                if (
                    adf.artist_data.country_code == "GB"
                    and adf.artist_data.city == "Liverpool"
                    and adf.artist_data.region_code != "GB-NWK"
                ):
                    reformat_applicable = True
                    adf.artist_data.region_code = "GB-NWK"

                if (
                    adf.artist_data.country_code == "GB"
                    and adf.artist_data.city == "London"
                    and adf.artist_data.region_code != "GB-LND"
                ):
                    reformat_applicable = True
                    adf.artist_data.region_code = "GB-LND"

                if (
                    adf.artist_data.country_code == "GB"
                    and adf.artist_data.city == "Londres"
                ):
                    reformat_applicable = True
                    adf.artist_data.city = "London"
                    adf.artist_data.region_code = "GB-LND"

                if (
                    adf.artist_data.country_code == "GB"
                    and adf.artist_data.city == "Sheffield"
                    and adf.artist_data.region_code != "GB-SHF"
                ):
                    reformat_applicable = True
                    adf.artist_data.region_code = "GB-SHF"

                if (
                    adf.artist_data.country_code == "FR"
                    and adf.artist_data.city == "Paris"
                    and adf.artist_data.region_code != "FR-IDF"
                ):
                    reformat_applicable = True
                    adf.artist_data.region_code = "FR-IDF"

                if (
                    adf.artist_data.country_code == "MX"
                    and adf.artist_data.city == "Mexico City"
                    and adf.artist_data.region_code != "MX-CMX"
                ):
                    reformat_applicable = True
                    adf.artist_data.region_code = "MX-CMX"

                if (
                    adf.artist_data.country_code == "MX"
                    and adf.artist_data.region_code == "MX-CMX"
                    and adf.artist_data.city != "Mexico City"
                ):
                    reformat_applicable = True
                    adf.artist_data.city = "Mexico City"

                # consolidating the boroughs so that the NYC numbers are comparable to LA and London
                # yes, I'm including the "sixth borough" (Yonkers)
                # also including Nyack for the same reason (it's part of the NYC metropolitan area)
                if (
                    adf.artist_data.country_code == "US"
                    and adf.artist_data.region_code == "US-NY"
                    and adf.artist_data.city
                    in (
                        "New York",
                        "New York City, NY",
                        "Brooklyn",
                        "The Bronx",
                        "Long Island",
                        "Manhattan",
                        "Queens",
                        "Staten Island",
                        "Yonkers",
                        "Nyack",
                    )
                ):
                    reformat_applicable = True
                    adf.artist_data.city = "New York City"

                if reformat_applicable:
                    yaml_str: str = adf.to_yaml(allow_unicode=True)
                    # print(yaml_str)
                    with open(artist_yaml_path, "w") as f:
                        f.write(yaml_str)
                    print(f"Updated {artist_yaml_path}")
                    artists_updated.append(artist_yaml_path)
                    # sys.exit(0)
            except Exception as ex:
                artists_missing.append(artist)
                exceptions.append((artist_yaml_path, ex))
    exist_count = len(artist_paths) - len(artists_missing)

    print(
        f"Found artist.yml for {exist_count} out of {len(artist_paths)} artist folders"
    )
    print(f"Missing count: {len(artists_missing)}")
    # print("Writing artists missing artist.yml list to artists_missing.txt")
    # with open("artists_missing.txt", "w") as f:
    #    for artist in sorted(artists_missing):
    #        f.write(artist + os.linesep)
    print(f"Total artist.yml files updated: {len(artists_updated)}")
    print("Exceptions:")
    for artist_yaml_path, ex in exceptions:
        print(artist_yaml_path, ex)


if __name__ == "__main__":
    main()
