"""Populate track YAML mediaFileData from matching media files."""

import argparse
from io import StringIO
import logging
import os
import shlex
import tempfile
from collections.abc import Mapping, MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, cast

import mutagen
from mediascan.types.track.track_yaml_file_validator import is_mapping, is_list
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from mediascripts.track.track_yaml.track_yaml_common import (
    TextWriter,
    iter_collection_files,
    save_yaml_atomically,
    show_yaml_file_diff,
)

logger = logging.getLogger(__name__)

EXCLUDED_TRACK_YAML_NAMES = {"artist", "album", "files", "artists"}
TRACK_YAML_SUFFIXES = {".yml", ".yaml"}


class _RoundTripYaml(Protocol):
    def load(self, stream: Path) -> object: ...
    def dump(self, data: Mapping[str, object], stream: TextWriter) -> None: ...


class _AudioInfo(Protocol):
    length: float


class _Audio(Protocol):
    info: _AudioInfo
    tags: Mapping[str, list[str]] | None


def _read_tag(tags: Mapping[str, list[str]], key: str, fallback: str) -> str:
    values = tags.get(key)
    if values and values[0].strip():
        return values[0].strip()
    return fallback


def _read_year(tags: Mapping[str, list[str]], fallback: int) -> int:
    value = _read_tag(tags, "date", str(fallback))
    try:
        return int(value[:4])
    except ValueError:
        logger.warning("Ignoring invalid media-file date: %r", value)
        return fallback


def _collection_relative_path(media_path: Path) -> str:
    collection_root = os.environ.get("MOONGAS_COLLECTION_ROOTDIR")
    if not collection_root:
        raise ValueError("MOONGAS_COLLECTION_ROOTDIR must be set to write collection-relative media paths")
    absolute_root = Path(os.path.abspath(collection_root))
    absolute_media_path = Path(os.path.abspath(media_path))
    try:
        relative_path = absolute_media_path.relative_to(absolute_root)
    except ValueError as error:
        raise ValueError(f"Media file is outside the collection root: {media_path}") from error
    return f"/{relative_path.as_posix()}"


def find_media_file(track_yaml_path: Path) -> Path:
    candidates = sorted(
        candidate
        for candidate in track_yaml_path.parent.iterdir()
        if candidate.is_file()
        and candidate.stem == track_yaml_path.stem
        and candidate.suffix.lower() not in TRACK_YAML_SUFFIXES
    )
    if not candidates:
        raise FileNotFoundError(f"No media file with basename '{track_yaml_path.stem}' beside {track_yaml_path}")

    media_errors: list[str] = []
    for candidate in candidates:
        try:
            audio = mutagen.File(candidate, easy=True)
        except mutagen.MutagenError as error:
            media_errors.append(f"{candidate}: {error}")
            continue
        if audio is not None:
            return candidate
    details = "; ".join(media_errors)
    message = f"No readable media file with basename '{track_yaml_path.stem}' beside {track_yaml_path}"
    if details:
        message = f"{message}: {details}"
    raise ValueError(message)


def process_track_yaml(
    track_yaml_path: Path,
    *,
    dry_run: bool = False,
) -> bool:
    try:
        media_path = find_media_file(track_yaml_path)
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 4096
        round_trip_yaml = cast(_RoundTripYaml, yaml)
        original = track_yaml_path.read_text(encoding="utf-8")
        document = cast(MutableMapping[str, object], round_trip_yaml.load(track_yaml_path))

        key = "media_file_data" if "media_file_data" in document and "mediaFileData" not in document else "mediaFileData"
        media_file_data = document.get(key)
        if media_file_data is None:
            entries: list[object] = []
        elif is_list(media_file_data):
            entries = media_file_data
            if len(entries) > 1:
                raise ValueError("Expected at most one mediaFileData entry per track")
        else:
            raise ValueError("Expected mediaFileData to be a list")

        if entries:
            entry: object = entries[0]
            if not is_mapping(entry):
                raise ValueError("Expected mediaFileData[0] to be a mapping")
            media_file = entry
        else:
            media_file: MutableMapping[str, object] = {}
            entries.append(media_file)
            document[key] = entries

        track_data = document.get("trackData", document.get("track_data"))
        if not is_mapping(track_data):
            raise ValueError("Expected a trackData mapping")
        track_values = track_data
        for field in ("title", "artist", "albumartist", "album", "genre"):
            value = track_values.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"trackData.{field} must be a non-empty string")
        year = track_values.get("year")
        if type(year) is not int:
            raise ValueError("trackData.year must be an integer")

        loaded_audio = mutagen.File(media_path, easy=True)
        if loaded_audio is None:
            raise ValueError(f"Could not read media metadata from {media_path}")
        audio = cast(_Audio, loaded_audio)
        tags = audio.tags or {}
        file_stat = media_path.stat()
        media_values: dict[str, object] = {
            "path": _collection_relative_path(media_path),
            "size": file_stat.st_size,
            "format": media_path.suffix.lstrip(".").lower(),
            "title": _read_tag(tags, "title", cast(str, track_values["title"])),
            "artist": _read_tag(tags, "artist", cast(str, track_values["artist"])),
            "albumartist": _read_tag(tags, "albumartist", cast(str, track_values["albumartist"])),
            "album": _read_tag(tags, "album", cast(str, track_values["album"])),
            "genre": _read_tag(tags, "genre", cast(str, track_values["genre"])),
            "year": _read_year(tags, year),
            "duration": audio.info.length,
            "modTime": datetime.fromtimestamp(file_stat.st_mtime, timezone.utc),
        }
        changed = False
        for field in ("albumPath", "artistPath"):
            if field in media_file:
                del media_file[field]
                changed = True
        for field, value in media_values.items():
            if media_file.get(field) != value:
                media_file[field] = value
                changed = True
        if not changed:
            logger.info("[%s] MediaFileData already matches %s", track_yaml_path, media_path)
            return True

        proposal_stream = StringIO()
        round_trip_yaml.dump(document, proposal_stream)
        proposed = proposal_stream.getvalue()
        show_yaml_file_diff(track_yaml_path, original, proposed)
        if dry_run:
            proposal_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix="media-file-data-proposal-",
                    suffix=track_yaml_path.suffix,
                    delete=False,
                ) as proposal:
                    proposal_path = Path(proposal.name)
                    proposal.write(proposed)
            except OSError:
                if proposal_path is not None:
                    proposal_path.unlink(missing_ok=True)
                raise
            logger.info("[%s] [dry-run] Source and backups unchanged", track_yaml_path)
            logger.info("Proposal retained at %s; remove it when finished", proposal_path)
            print(shlex.join(["meld", str(track_yaml_path.resolve()), str(proposal_path)]), flush=True)
        else:
            def write_proposed_yaml(stream: TextWriter) -> None:
                stream.write(proposed)

            save_yaml_atomically(
                track_yaml_path,
                write_proposed_yaml,
                track_yaml_path.with_name(f"{track_yaml_path.name}.bak"),
            )
            logger.info("[%s] Updated MediaFileData from %s", track_yaml_path, media_path)
            print(
                shlex.join(
                    [
                        "meld",
                        str(track_yaml_path.with_name(f"{track_yaml_path.name}.bak").resolve()),
                        str(track_yaml_path.resolve()),
                    ]
                ),
                flush=True,
            )
        return True
    except (OSError, UnicodeError, YAMLError, ValueError, TypeError, mutagen.MutagenError) as error:
        logger.error("[%s] Could not update MediaFileData: %s", track_yaml_path, error)
        return False


def _collection_data_path(specific_dir: Path | None) -> Path:
    if specific_dir is not None:
        path = specific_dir
    else:
        collection_root = os.environ.get("MOONGAS_COLLECTION_ROOTDIR")
        if not collection_root:
            raise ValueError("MOONGAS_COLLECTION_ROOTDIR must be set, or use --file/--dir")
        path = Path(collection_root) / "data"
    if not path.is_dir():
        raise ValueError(f"Input directory does not exist: {path}")
    return path


def load_track_yaml_files(path: Path, album_directories_only: bool = True) -> list[Path]:
    return sorted(
        file
        for file in iter_collection_files(path, album_directories_only)
        if file.suffix.lower() in TRACK_YAML_SUFFIXES
        and file.stem.lower() not in EXCLUDED_TRACK_YAML_NAMES
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--file", type=Path, help="Process one existing track YAML file.")
    scope.add_argument("-d", "--dir", type=Path, help="Recursively scan a directory instead of the collection.")
    parser.add_argument("-c", "--count", type=int, help="Process at most this many track YAML files.")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Show proposals without changing YAML or backups.")
    args = parser.parse_args()
    if args.count is not None and args.count < 0:
        parser.error("--count must be non-negative")
    logging.basicConfig(level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
    try:
        if args.file is not None:
            if not args.file.is_file():
                raise ValueError(f"Specified file does not exist: {args.file}")
            if args.file.suffix.lower() not in TRACK_YAML_SUFFIXES:
                raise ValueError(f"Specified file is not a YAML track file: {args.file}")
            track_yaml_files = [args.file]
        else:
            track_yaml_files = load_track_yaml_files(
                _collection_data_path(args.dir), album_directories_only=args.dir is None
            )
        if args.count is not None:
            track_yaml_files = track_yaml_files[:args.count]
        logger.info("Processing %d track YAML file(s)", len(track_yaml_files))
        succeeded = True
        for track_yaml_path in track_yaml_files:
            if not process_track_yaml(track_yaml_path, dry_run=args.dry_run):
                succeeded = False
        return 0 if succeeded else 1
    except ValueError as error:
        logger.error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
