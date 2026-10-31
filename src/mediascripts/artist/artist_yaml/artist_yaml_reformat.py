"""Batch update known artist metadata corrections in artist.yml files."""

import argparse
from pathlib import Path

from mediascan.types.artist.artist_yaml_file_loader import load_artist_yaml_file
from mediascripts.artist.artist_yaml.artist_yaml_common import (
    excluded,
    load_artist_paths_from_database,
    print_artist_yaml_summary,
)

REFORMAT_ALL = False


def reformat_artist_yaml(artist_yaml_path: Path, reformat_all: bool = False) -> bool:
    artist_file = load_artist_yaml_file(artist_yaml_path)
    artist_data = artist_file.artist_data
    reformat_applicable = REFORMAT_ALL or reformat_all

    region_corrections = {
        ("GB", "Birmingham"): "GB-WMD",
        ("GB", "Manchester"): "GB-NWK",
        ("GB", "Liverpool"): "GB-NWK",
        ("GB", "London"): "GB-LND",
        ("GB", "Sheffield"): "GB-SHF",
        ("FR", "Paris"): "FR-IDF",
        ("MX", "Mexico City"): "MX-CMX",
    }
    region_code = region_corrections.get((artist_data.country_code, artist_data.city))
    if region_code and artist_data.region_code != region_code:
        artist_data.region_code = region_code
        reformat_applicable = True

    if artist_data.country_code == "GB" and artist_data.city == "Londres":
        artist_data.city = "London"
        artist_data.region_code = "GB-LND"
        reformat_applicable = True

    if (
        artist_data.country_code == "MX"
        and artist_data.region_code == "MX-CMX"
        and artist_data.city != "Mexico City"
    ):
        artist_data.city = "Mexico City"
        reformat_applicable = True

    new_york_cities = {
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
    }
    if (
        artist_data.country_code == "US"
        and artist_data.region_code == "US-NY"
        and artist_data.city in new_york_cities
    ):
        if artist_data.city != "New York City":
            artist_data.city = "New York City"
            reformat_applicable = True

    if reformat_applicable:
        artist_yaml_path.write_text(artist_file.to_yaml(allow_unicode=True), encoding="utf-8")
    return reformat_applicable


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch modifies artist.yml files to make changes to the YAML format."
    )
    parser.add_argument(
        "--reformat-all",
        action="store_true",
        help="Reformat all artist.yml files, not just those matching the built-in rules",
    )
    args = parser.parse_args()
    artist_paths = load_artist_paths_from_database(parser)
    artists_missing: list[str] = []
    artists_updated: list[Path] = []
    exceptions: list[tuple[Path, Exception]] = []
    for artist, artist_path in artist_paths.items():
        if excluded(artist_path):
            continue

        artist_yaml_path = artist_path / "artist.yml"
        if not artist_yaml_path.exists():
            print(f"{artist_path} missing artist.yml")
            artists_missing.append(artist)
            continue

        try:
            if reformat_artist_yaml(artist_yaml_path, args.reformat_all):
                print(f"Updated {artist_yaml_path}")
                artists_updated.append(artist_yaml_path)
        except Exception as exception:
            artists_missing.append(artist)
            exceptions.append((artist_yaml_path, exception))

    print_artist_yaml_summary(len(artist_paths), len(artists_missing), "Missing count")
    print(f"Total artist.yml files updated: {len(artists_updated)}")
    print("Exceptions:")
    for artist_yaml_path, exception in exceptions:
        print(artist_yaml_path, exception)


if __name__ == "__main__":
    main()
