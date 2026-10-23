"""Interactively resolve differences between track YAML and MP3 ID3 tags.

Use the collection configured by MOONGAS_COLLECTION_ROOTDIR, or recursively scan
-d / --dir DIRECTORY. Artist exclusions and comparison rules match the comparison
script. -q / --quiet hides matching tracks, never conflict prompts.

For each conflicting field, type y (Accept Track YAML), m (Accept Media File
Tags), e (Enter value, updates both files), or s (Skip), then Enter.
Empty input accepts Track YAML. Each accepted change is saved
immediately; Ctrl+C or EOF stops without changing the current conflict.
Exit status: 0 if resolved, 1 if skipped/errors, 130 if interrupted.
"""

import logging
import os
import re
import stat
import tempfile
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Protocol, cast

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from mediascripts.track.track_yaml.track_yaml_common import (
    TAG_FIELDS,
    TRACK_ERRORS,
    load_track_metadata,
    run_cli,
    tag_values,
)

logger = logging.getLogger(__name__)


class _TextWriter(Protocol):
    def write(self, text: str, /) -> int: ...


class _RoundTripYaml(Protocol):
    def load(self, stream: Path) -> object: ...
    def dump(self, data: Mapping[str, object], stream: _TextWriter) -> None: ...


class _TagWriter(Protocol):
    def save(self, filething: Path) -> None: ...


def prompt_resolution(track_path: Path, field: str, expected: str, actual: list[str]) -> str:
    print(f"\n{track_path}\n  {field}: Track YAML = {expected!r}; Media File Tags = {actual!r}")
    while True:
        choice = input(
            "[y] Accept Track YAML (default)  [m] Accept Media File Tags  [e] Enter value  [s] Skip: "
        ).strip().lower()
        if choice in ("y", "m", "e", "s"):
            return choice
        if not choice:
            return "y"
        print("Enter y, m, e, or s, then press Enter.")


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
    yaml = YAML()
    yaml.preserve_quotes = True
    round_trip_yaml = cast(_RoundTripYaml, yaml)
    document = cast(MutableMapping[str, object], round_trip_yaml.load(yaml_path))
    data = document.get("trackData", document.get("track_data"))
    if not isinstance(data, MutableMapping):
        raise ValueError("Expected a trackData mapping")
    cast(MutableMapping[str, object], data)[field] = value
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=yaml_path.parent,
            prefix=f".{yaml_path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            round_trip_yaml.dump(document, stream)
        temporary_path.chmod(stat.S_IMODE(yaml_path.stat().st_mode))
        os.replace(temporary_path, yaml_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def merge_track(track_path: Path, quiet: bool = False) -> bool:
    try:
        metadata = load_track_metadata(track_path)
        if metadata is None:
            return True
        track_data, tags = metadata
        writable_tags = cast(MutableMapping[str, list[str]], tags)
        resolved = True
        differences = False
        for field, tag in TAG_FIELDS.items():
            expected = str(getattr(track_data, field)).strip()
            actual = tag_values(tags, field)
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
        return resolved
    except (*TRACK_ERRORS, YAMLError) as error:
        logger.error("[%s] Could not merge: %s", track_path, error)
        return False


def main() -> int:
    try:
        return run_cli(__doc__, merge_track)
    except (KeyboardInterrupt, EOFError):
        logger.warning("Merge interrupted; previously accepted changes have been saved")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
