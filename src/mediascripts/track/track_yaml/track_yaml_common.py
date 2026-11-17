"""Shared metadata comparison, scanning, atomic writes, diffs, and CLI for track YAML scripts."""

import argparse
import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from difflib import unified_diff
from pathlib import Path
from typing import Protocol, cast

import mutagen
from dataclass_wizard import fromdict
from dataclass_wizard.v0.errors import JSONWizardError
from mediascan.types.track.track_data import TrackData
from mediascan.types.track.track_yaml_file_validator import is_mapping
from mediascan.types.album.album_path import AlbumPathBuilder
from mutagen.easyid3 import EasyID3
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError as RuamelYAMLError
from yaml import YAMLError as PyYAMLError

logger = logging.getLogger(__name__)

BOOLEAN_LIKE_STRINGS = frozenset(
    {
        "y", "Y", "yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON",
        "n", "N", "no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF",
    }
)
TAG_FIELDS = {
    "title": "title",
    "artist": "artist",
    "albumartist": "albumartist",
    "album": "album",
    "genre": "genre",
    "year": "date",
}
TRACK_ERRORS = (
    OSError, UnicodeError, PyYAMLError, RuamelYAMLError, JSONWizardError,
    ValueError, TypeError, mutagen.MutagenError,
)


class TextWriter(Protocol):
    def write(self, text: str, /) -> int: ...


class _YamlRepresenter(Protocol):
    def represent_scalar(self, tag: str, value: str, style: str | None = None) -> object: ...


def _represent_track_yaml_string(representer: _YamlRepresenter, value: str) -> object:
    style = '"' if value in BOOLEAN_LIKE_STRINGS else None
    return representer.represent_scalar("tag:yaml.org,2002:str", value, style=style)


def track_yaml(typ: str = "rt", *, preserve_quotes: bool = False) -> YAML:
    """Create a YAML handler that quotes boolean-like strings."""
    yaml = YAML(typ=typ)
    yaml.preserve_quotes = preserve_quotes
    yaml.default_flow_style = False
    yaml.representer.add_representer(str, _represent_track_yaml_string)
    return yaml


def save_yaml_atomically(
    yaml_path: Path, write: Callable[[TextWriter], None], backup_path: Path | None = None,
    *, create_only: bool = False,
) -> None:
    """Stage YAML atomically; optionally create a new file without overwriting."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=yaml_path.parent,
            prefix=f".{yaml_path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            write(stream)
        if create_only:
            os.link(temporary_path, yaml_path)
        else:
            temporary_path.chmod(stat.S_IMODE(yaml_path.stat().st_mode))
            if backup_path is not None:
                shutil.copy2(yaml_path, backup_path)
            os.replace(temporary_path, yaml_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def format_field_diff(field: str, original: str, updated: str, updated_label: str) -> str:
    return f"  {field}: Track YAML = {original}; {updated_label} = {updated}"


def show_yaml_file_diff(path: Path, original: str, proposed: str) -> None:
    """Print a unified diff, including explicit missing-final-newline markers."""
    lines = unified_diff(
        original.splitlines(keepends=True), proposed.splitlines(keepends=True),
        fromfile=str(path), tofile=f"{path} (proposed)",
    )
    for line in lines:
        print(line, end="" if line.endswith("\n") else "\n", flush=True)
        if not line.endswith("\n"):
            print("\\ No newline at end of file", flush=True)


def report_walk_error(error: OSError) -> None:
    raise error


def iter_collection_files(
    path: Path, album_directories_only: bool = True,
    onerror: Callable[[OSError], None] = report_walk_error,
) -> Iterator[Path]:
    """Walk library symlinks, optionally restricting files to album directories."""
    for root, _, filenames in os.walk(path, followlinks=True, onerror=onerror):
        directory = Path(root)
        if album_directories_only and not AlbumPathBuilder.of(directory).valid:
            continue
        for filename in sorted(filenames):
            yield directory / filename


def load_track_metadata(track_path: Path) -> tuple[TrackData, EasyID3] | None:
    """Load core track metadata only, or return None for an excluded artist."""
    exclude_keywords = [
        keyword for keyword in os.environ.get("COPY_MEDIALIB_EXCLUDE_KEYWORDS", "").splitlines()
        if keyword.strip()
    ]
    artist_dirname = AlbumPathBuilder.of(track_path.parent).artist_dirname
    if any(keyword in artist_dirname for keyword in exclude_keywords):
        logger.debug("[%s] Skipping excluded artist directory %r", track_path, artist_dirname)
        return None
    yaml_path = track_path.with_suffix(".yml")
    with yaml_path.open(encoding="utf-8") as stream:
        document: object = track_yaml(typ="safe").load(stream)
    if not is_mapping(document):
        raise ValueError("Expected a track YAML mapping, not a sequence")
    data = document.get("trackData", document.get("track_data"))
    if not is_mapping(data):
        raise ValueError("Expected a trackData mapping")
    track_data = fromdict(TrackData, data)
    if any(keyword in track_data.artist for keyword in exclude_keywords):
        logger.debug("[%s] Skipping excluded artist %r", track_path, track_data.artist)
        return None
    return track_data, EasyID3(track_path)


def tag_values(tags: EasyID3, field: str) -> list[str]:
    values = [
        value.strip() for value in cast(Mapping[str, list[str]], tags).get(TAG_FIELDS[field], [])
    ]
    if field == "year":
        return [
            value[:4] if re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value) else value
            for value in values
        ]
    return values


def track_field_values(track_data: TrackData, tags: EasyID3, field: str) -> tuple[str, list[str]]:
    return str(getattr(track_data, field)).strip(), tag_values(tags, field)


def process_collection(
    collection_data_path: Path, track_handler: Callable[[Path, bool], bool],
    quiet: bool = False, album_directories_only: bool = True,
) -> bool:
    matches = True

    def report_walk_error(error: OSError) -> None:
        nonlocal matches
        matches = False
        logger.error("Could not scan collection: %s", error)

    for track_path in iter_collection_files(collection_data_path, album_directories_only, report_walk_error):
        if track_path.suffix.lower() == ".mp3":
            if not track_handler(track_path, quiet):
                matches = False
    return matches


def run_cli(description: str | None, track_handler: Callable[[Path, bool], bool]) -> int:
    parser = argparse.ArgumentParser(description=description)
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
        return 0 if process_collection(
            args.dir, quiet=args.quiet, album_directories_only=False, track_handler=track_handler
        ) else 1
    collection_root = os.environ.get("MOONGAS_COLLECTION_ROOTDIR")
    if not collection_root:
        logger.error("MOONGAS_COLLECTION_ROOTDIR must be set")
        return 1
    data_path = Path(collection_root) / "data"
    if not data_path.is_dir():
        logger.error("Collection data directory does not exist: %s", data_path)
        return 1
    return 0 if process_collection(data_path, quiet=args.quiet, track_handler=track_handler) else 1
