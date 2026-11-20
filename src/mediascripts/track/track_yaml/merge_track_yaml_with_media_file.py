"""Interactively resolve differences between track YAML and MP3 ID3 tags.

Use the collection configured by MOONGAS_COLLECTION_ROOTDIR, or recursively scan
-d / --dir DIRECTORY. Artist exclusions and comparison rules match the comparison
script. -q / --quiet hides matching tracks, never conflict prompts.
MOONGAS_COLLECTION_ROOTDIR is required even with --dir; new media paths are
stored relative to that root with a leading slash (for example /data/Music/...).

For each conflicting field, press y (Accept Track YAML), m (Accept Media File
Tags), e (Enter value, updates both files), or s (Skip), without pressing Enter.
Enter accepts Track YAML. Custom values still require Enter. Redirected input
uses line-based choices. Each accepted change is saved
immediately; Ctrl+C or EOF stops without changing the current conflict.
Exit status: 0 if resolved, 1 if skipped/errors, 130 if interrupted.
"""

import logging
import os
import re
import sys
from collections.abc import Mapping, MutableMapping
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, cast

import mutagen
import readchar
from ruamel.yaml.error import YAMLError

from mediascan.types.track.track_data import TrackData
from mediascan.types.mediafile.mediafile_data import MediaFileData
from mediascan.types.track.track_yaml_file_validator import camel_case
from mediascripts.track.track_yaml.track_yaml_common import (
    TAG_FIELDS,
    TRACK_ERRORS,
    TextWriter,
    collection_relative_path,
    format_field_diff,
    load_track_metadata,
    run_cli,
    save_yaml_atomically,
    track_yaml,
    track_field_values,
)

logger = logging.getLogger(__name__)


class _RoundTripYaml(Protocol):
    def load(self, stream: Path) -> object: ...
    def dump(self, data: Mapping[str, object], stream: TextWriter) -> None: ...


class _TagWriter(Protocol):
    def save(self, filething: Path) -> None: ...


class _AudioInfo(Protocol):
    length: float


class _Audio(Protocol):
    info: _AudioInfo


def read_choice(prompt: str) -> str:
    if not sys.stdin.isatty():
        return input(prompt).strip()
    print(prompt, end="", flush=True)
    choice = readchar.readkey()
    if choice == "\x03":
        raise KeyboardInterrupt
    if choice in ("", "\x04", "\x1a"):
        raise EOFError
    print(choice if choice in "yYmMeEsS" else "")
    return "" if choice in ("\r", "\n") else choice


def prompt_resolution(track_path: Path, field: str, expected: str, actual: list[str]) -> str:
    print(f"\n{track_path}\n" + format_field_diff(field, repr(expected), repr(actual), "Media File Tags"))
    while True:
        choice = read_choice(
            "[y] Accept Track YAML (default)  [m] Accept Media File Tags  [e] Enter value  [s] Skip: "
        ).lower()
        if choice in ("y", "m", "e", "s"):
            return choice
        if not choice:
            return "y"
        print("Press y, m, e, or s (no Enter needed).")


def prompt_custom_value(field: str) -> str:
    while True:
        value = input(f"Enter value for {field}: ").strip()
        if not value:
            print("A custom value cannot be empty.")
        elif field == "year" and not re.fullmatch(r"[0-9]{4}", value):
            print("Enter a four-digit year.")
        else:
            return value


def save_yaml_value(yaml_path: Path, field: str, value: str | int) -> None:
    """Round-trip YAML through a sibling temporary file to avoid partial writes."""
    yaml = track_yaml(preserve_quotes=True)
    round_trip_yaml = cast(_RoundTripYaml, yaml)
    document = cast(MutableMapping[str, object], round_trip_yaml.load(yaml_path))
    data = document.get("trackData", document.get("track_data"))
    if not isinstance(data, MutableMapping):
        raise ValueError("Expected a trackData mapping")
    cast(MutableMapping[str, object], data)[field] = value
    save_yaml_atomically(yaml_path, lambda stream: round_trip_yaml.dump(document, stream))


def add_missing_media_file_data(
    yaml_path: Path,
    track_path: Path,
    track_data: TrackData,
    tags: Mapping[str, list[str]],
) -> bool:
    """Fill absent media-file metadata while preserving existing YAML values."""
    yaml = track_yaml(preserve_quotes=True)
    round_trip_yaml = cast(_RoundTripYaml, yaml)
    document = cast(MutableMapping[str, object], round_trip_yaml.load(yaml_path))
    key = "media_file_data" if "media_file_data" in document and "mediaFileData" not in document else "mediaFileData"
    media_file_data = document.get(key)
    if media_file_data is None:
        entries: list[object] = []
    elif isinstance(media_file_data, list):
        entries = cast(list[object], media_file_data)
        if len(entries) > 1:
            raise ValueError("Expected at most one mediaFileData entry per track")
    else:
        raise ValueError("Expected mediaFileData to be a list")

    if entries:
        entry = entries[0]
        if not isinstance(entry, MutableMapping):
            raise ValueError("Expected mediaFileData[0] to be a mapping")
        media_file = cast(MutableMapping[str, object], entry)
    else:
        media_file: MutableMapping[str, object] = {}
        entries.append(media_file)
        document[key] = entries

    file_stat = track_path.stat()
    metadata = MediaFileData(
        path=collection_relative_path(track_path),
        size=file_stat.st_size,
        format=track_path.suffix.lstrip(".").lower(),
        title=next(iter(tags.get("title", [])), track_data.title),
        artist=next(iter(tags.get("artist", [])), track_data.artist),
        albumartist=next(iter(tags.get("albumartist", [])), track_data.albumartist),
        album=next(iter(tags.get("album", [])), track_data.album),
        genre=next(iter(tags.get("genre", [])), track_data.genre),
        year=_tag_year(next(iter(tags.get("date", [])), str(track_data.year)), track_data.year),
        mod_time=datetime.fromtimestamp(file_stat.st_mtime, timezone.utc),
    )
    if media_file.get("duration") is None:
        try:
            audio = cast(_Audio | None, mutagen.File(track_path))
        except mutagen.MutagenError as error:
            logger.warning("[%s] Could not read media duration: %s", track_path, error)
            audio = None
        if audio is not None:
            metadata.duration = audio.info.length

    changed = False
    for obsolete in ("albumPath", "artistPath", "album_path", "artist_path"):
        if obsolete in media_file:
            del media_file[obsolete]
            changed = True
    for field in fields(metadata):
        value: object = getattr(metadata, field.name)
        key = camel_case(field.name)
        if value is not None and media_file.get(key) is None and media_file.get(field.name) is None:
            media_file[key] = value
            changed = True

    if changed:
        save_yaml_atomically(yaml_path, lambda stream: round_trip_yaml.dump(document, stream))
    return changed


def _tag_year(value: str, fallback: int) -> int:
    match = re.match(r"\d{4}", value)
    return int(match.group()) if match else fallback


def merge_track(track_path: Path, quiet: bool = False) -> bool:
    try:
        metadata = load_track_metadata(track_path)
        if metadata is None:
            return True
        collection_relative_path(track_path)
        track_data, tags = metadata
        writable_tags = cast(MutableMapping[str, list[str]], tags)
        resolved = True
        differences = False
        for field, tag in TAG_FIELDS.items():
            expected, actual = track_field_values(track_data, tags, field)
            if actual == [expected]:
                continue
            differences = True
            while True:
                choice = prompt_resolution(track_path, field, expected, writable_tags.get(tag, []))
                if choice != "m":
                    break
                if len(actual) != 1 or not actual[0]:
                    logger.warning(
                        "[%s] Cannot copy missing/multiple ID3 %s values into a single YAML field; choose y, e, or s",
                        track_path, tag,
                    )
                    continue
                if field == "year" and not re.fullmatch(r"\d{4}", actual[0]):
                    logger.warning("[%s] ID3 date has no valid four-digit year; choose y, e, or s", track_path)
                    continue
                break
            if choice == "y":
                logger.info(
                    "[%s] Copying Track YAML %s=%r to MP3 ID3 %s (previous=%r)",
                    track_path, field, expected, tag, writable_tags.get(tag, []),
                )
                writable_tags[tag] = [expected]
                cast(_TagWriter, tags).save(track_path)
                logger.info("[%s] Saved MP3 ID3 %s=%r from Track YAML", track_path, tag, expected)
            elif choice == "m":
                value = int(actual[0]) if field == "year" else actual[0]
                save_yaml_value(track_path.with_suffix(".yml"), field, value)
                logger.info("[%s] Updated YAML %s from Media File Tags", track_path, field)
            elif choice == "e":
                custom = prompt_custom_value(field)
                logger.info(
                    "[%s] Applying custom %s=%r to Track YAML and MP3 ID3 %s",
                    track_path, field, custom, tag,
                )
                save_yaml_value(
                    track_path.with_suffix(".yml"), field,
                    int(custom) if field == "year" else custom,
                )
                logger.info("[%s] Saved YAML %s=%r; saving MP3 ID3 next", track_path, field, custom)
                writable_tags[tag] = [custom]
                cast(_TagWriter, tags).save(track_path)
                logger.info("[%s] Saved custom %s=%r to both Track YAML and MP3 ID3", track_path, field, custom)
            else:
                resolved = False
                logger.info("[%s] Skipped %s", track_path, field)
        if not differences and not quiet:
            logger.info("[%s] Track YAML matches media file tags", track_path)
        if add_missing_media_file_data(track_path.with_suffix(".yml"), track_path, track_data, writable_tags):
            logger.info("[%s] Added missing MediaFileData fields", track_path)
        return resolved
    except (*TRACK_ERRORS, YAMLError) as error:
        logger.error("[%s] Could not merge: %s", track_path, error)
        return False


def main() -> int:
    if not os.environ.get("MOONGAS_COLLECTION_ROOTDIR"):
        logger.error("MOONGAS_COLLECTION_ROOTDIR must be set to write collection-relative media paths")
        return 1
    try:
        return run_cli(__doc__, merge_track)
    except (KeyboardInterrupt, EOFError):
        logger.warning("Merge interrupted; previously accepted changes have been saved")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
