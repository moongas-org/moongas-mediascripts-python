"""Compare sibling <track>.yml metadata with MP3 ID3 tags without changing files.

Walk album directories under $MOONGAS_COLLECTION_ROOTDIR/data, following library
symlinks. Compare the six core trackData fields; streaming identifiers and extra
ID3 tags are outside this comparison. ID3 dates are compared by year, and leading
or trailing whitespace is ignored. Quiet mode suppresses matching-track messages.
Tracks whose artist directory or YAML artist contains a newline-separated keyword from
COPY_MEDIALIB_EXCLUDE_KEYWORDS are ignored (case-sensitive; blank lines ignored).

Use -d / --dir to recursively scan an arbitrary directory instead, without
requiring MOONGAS_COLLECTION_ROOTDIR or a collection directory structure.

Usage: compare-track-yaml-with-media-file [-q | --quiet] [-d DIRECTORY]
Exit status: 0 when all tracks match, 1 on differences or errors.
"""

import argparse
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import mutagen
from dataclass_wizard.v0.errors import JSONWizardError
from mediascan.track_yaml_file import TrackYamlFile
from mediascan.track_yaml_file_loader import load_track_yaml_file
from mediascan.utils.path.album_path import AlbumPathBuilder
from mutagen.easyid3 import EasyID3
from yaml import YAMLError

logger = logging.getLogger(__name__)

TAG_FIELDS = {
    "title": "title",
    "artist": "artist",
    "albumartist": "albumartist",
    "album": "album",
    "genre": "genre",
    "year": "date",
}


def compare_track(track_path: Path, quiet: bool = False) -> bool:
    """Report differences and return whether all core fields match."""
    exclude_keywords = [
        keyword for keyword in os.environ.get("COPY_MEDIALIB_EXCLUDE_KEYWORDS", "").splitlines()
        if keyword.strip()
    ]
    artist_dirname = AlbumPathBuilder.of(track_path.parent).artist_dirname
    if any(keyword in artist_dirname for keyword in exclude_keywords):
        logger.debug("[%s] Skipping excluded artist directory %r", track_path, artist_dirname)
        return True
    yaml_path = track_path.with_suffix(".yml")
    try:
        track_yaml = cast(object, load_track_yaml_file(yaml_path))
        if not isinstance(track_yaml, TrackYamlFile):
            raise ValueError("Expected a track YAML mapping, not a sequence")
        track_data = track_yaml.track_data
        if any(keyword in track_data.artist for keyword in exclude_keywords):
            logger.debug("[%s] Skipping excluded artist %r", track_path, track_data.artist)
            return True
        tags = cast(Mapping[str, list[str]], EasyID3(track_path))
    except (OSError, UnicodeError, YAMLError, JSONWizardError, ValueError, TypeError, mutagen.MutagenError) as error:
        logger.error("[%s] Could not compare %s: %s", track_path, yaml_path, error)
        return False

    matches = True
    for field, tag in TAG_FIELDS.items():
        expected = str(getattr(track_data, field)).strip()
        actual = [str(value).strip() for value in tags.get(tag, [])]
        if field == "year":
            actual = [
                value[:4] if re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value) else value
                for value in actual
            ]
        if actual != [expected]:
            matches = False
            logger.warning(
                "[%s] %s mismatch: YAML=%r, ID3 %s=%r",
                track_path, field, expected, tag, actual,
            )
    if matches and not quiet:
        logger.info("[%s] Track YAML matches media file tags", track_path)
    return matches


def compare_collection(
    collection_data_path: Path, quiet: bool = False, album_directories_only: bool = True
) -> bool:
    matches = True

    def report_walk_error(error: OSError) -> None:
        nonlocal matches
        matches = False
        logger.error("Could not scan collection: %s", error)

    for root, _, filenames in os.walk(
        collection_data_path, followlinks=True, onerror=report_walk_error
    ):
        album_path = Path(root)
        if album_directories_only and not AlbumPathBuilder.of(album_path).valid:
            continue
        for filename in sorted(filenames):
            track_path = album_path / filename
            if track_path.suffix.lower() == ".mp3":
                if not compare_track(track_path, quiet=quiet):
                    matches = False
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Only report differences and errors; suppress matching tracks.",
    )
    parser.add_argument(
        "-d", "--dir", type=Path,
        help="Recursively scan this directory instead of the configured collection.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.dir is not None:
        if not args.dir.is_dir():
            logger.error("Specified directory does not exist or is not a directory: %s", args.dir)
            return 1
        return 0 if compare_collection(
            args.dir, quiet=args.quiet, album_directories_only=False
        ) else 1
    collection_root = os.environ.get("MOONGAS_COLLECTION_ROOTDIR")
    if not collection_root:
        logger.error("MOONGAS_COLLECTION_ROOTDIR must be set")
        return 1
    data_path = Path(collection_root) / "data"
    if not data_path.is_dir():
        logger.error("Collection data directory does not exist: %s", data_path)
        return 1
    return 0 if compare_collection(data_path, quiet=args.quiet) else 1


if __name__ == "__main__":
    raise SystemExit(main())
