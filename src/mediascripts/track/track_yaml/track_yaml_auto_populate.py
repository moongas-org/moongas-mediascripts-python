"""Fill existing track YAML metadata and streaming identifiers with an LLM."""

import argparse
import logging
import os
import random
import shlex
import tempfile
import time
from collections.abc import Mapping
from dataclasses import fields
from io import StringIO
from pathlib import Path
from typing import Protocol, TypeGuard, cast

from mediascan.types.media_file.media_file_data import MediaFileData
from mediascan.types.track.streaming.streaming_data import StreamingData
from mediascan.types.track.track_data import TrackData
from mediascan.types.track.track_id_data import TrackIdData
from mediascan.types.track.track_yaml_file import TrackYamlFile
from mediascan.types.album.album_path import AlbumPathBuilder
from openai import APIError, APIStatusError, OpenAI
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from tqdm import tqdm

from mediascripts.common.cli.parse_boolean import parse_boolean
from mediascripts.common.openai.client import MODEL_NAME, get_client
from mediascripts.track.track_yaml.track_yaml_common import (
    TextWriter,
    format_field_diff,
    iter_collection_files,
    save_yaml_atomically,
    show_yaml_file_diff,
)

logger = logging.getLogger(__name__)


def camel_case(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.title() for part in rest)


CORE_FIELDS = {field.name for field in fields(TrackData)}
ID_FIELDS = {camel_case(field.name) for field in fields(TrackIdData)}
STREAMING_FIELDS = {camel_case(field.name) for field in fields(StreamingData)}
MEDIA_FILE_FIELDS = {camel_case(field.name) for field in fields(MediaFileData)}
FPCALC_FIELDS = {"duration", "fingerprint"}
YAML_ERRORS = (OSError, UnicodeError, YAMLError, ValueError, TypeError)

SYSTEM_PROMPT = f"""You are an automated track metadata-filling assistant.
Return ONLY valid YAML, without markdown fences, explanations, or comments.
Use the TrackYamlFile schema: a trackData mapping and an optional trackIdData mapping.
trackData contains title, artist, albumartist, album, genre (strings), and year (integer).
The trackIdData mapping may contain an optional streamingData list. Each streamingData
entry must have streamingService, trackUrl, trackId, apiData, and optional isrc.
Use streamingService values such as spotify, apple-music, deezer, and tidal.
Quote every identifier and URL, including numeric service IDs (e.g. trackId: "123456").
Fill missing core metadata and find ISRC and streaming identifiers/URLs for the exact
recording, artist, album, and version, not another recording with a similar title.
Preserve all existing non-empty metadata and identifiers. Never invent identifiers,
URLs, or facts. Omit fields you cannot establish; do not use placeholder strings.
trackIdData must be a mapping, never a list.
The optional mediaFileData list contains local-file metadata, fingerprints, and AcoustID data.
Preserve mediaFileData exactly; never invent, remove, or modify its entries.
"""


class _YamlLoader(Protocol):
    def load(self, stream: str) -> object: ...
    def dump(self, data: Mapping[str, object], stream: TextWriter) -> None: ...


def is_mapping(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(
        isinstance(key, str) for key in cast(dict[object, object], value)
    )


def is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def load_document(content: str) -> dict[str, object]:
    document = cast(_YamlLoader, YAML(typ="safe")).load(content)
    if not is_mapping(document):
        raise ValueError("Expected a track YAML mapping")
    return document


def normalize_model_identifiers(content: str) -> str:
    """Quote decimal service IDs without losing their original digits."""
    document = load_document(content)
    identifiers = document.get("trackIdData")
    if not is_mapping(identifiers):
        return content
    lexical = cast(_YamlLoader, YAML(typ="base")).load(content)
    if not is_mapping(lexical) or not is_mapping(lexical.get("trackIdData")):
        return content
    lexical_entry = cast(dict[str, object], lexical["trackIdData"])
    changed = False
    streaming = identifiers.get("streamingData")
    raw_streaming = lexical_entry.get("streamingData")
    if not is_list(streaming) or not is_list(raw_streaming):
        return content
    for streaming_index, item in enumerate(streaming):
        raw_item = raw_streaming[streaming_index]
        if not is_mapping(item) or not is_mapping(raw_item):
            continue
        value = item.get("trackId")
        raw_value = raw_item.get("trackId")
        if (
            type(value) is int
            and isinstance(raw_value, str)
            and raw_value.isascii()
            and raw_value.isdecimal()
        ):
            item["trackId"] = raw_value
            changed = True
            logger.warning(
                "Quoting numeric model identifier trackIdData.streamingData[%d].trackId: %r",
                streaming_index,
                raw_value,
            )
    if not changed:
        return content
    stream = StringIO()
    cast(_YamlLoader, YAML(typ="safe")).dump(document, stream)
    return stream.getvalue().strip()


def validate_track_yaml_content(content: str) -> dict[str, object]:
    """Enforce the camelCase mediascan schema before saving or skipping."""
    document = load_document(content)
    if document.keys() - {"trackData", "trackIdData", "mediaFileData"}:
        raise ValueError("Unexpected track YAML fields")
    core = document.get("trackData")
    if not is_mapping(core) or core.keys() != CORE_FIELDS:
        raise ValueError("trackData must contain exactly the six core metadata fields")
    for key, value in core.items():
        if key == "year":
            if type(value) is not int:
                raise ValueError("trackData.year must be an integer")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"trackData.{key} must be a non-empty string")
    identifiers = document.get("trackIdData")
    if identifiers is not None:
        if not is_mapping(identifiers) or identifiers.keys() - ID_FIELDS:
            raise ValueError("trackIdData must contain only supported identifier fields")
        streaming_data = identifiers.get("streamingData")
        if streaming_data is not None:
            if not is_list(streaming_data):
                raise ValueError("trackIdData.streamingData must be a list")
            for streaming_index, streaming in enumerate(streaming_data):
                field_path = f"trackIdData.streamingData[{streaming_index}]"
                if not is_mapping(streaming) or streaming.keys() - STREAMING_FIELDS:
                    raise ValueError(f"{field_path} must contain only supported streaming fields")
                for required in ("streamingService", "trackUrl", "trackId"):
                    value = streaming.get(required)
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(f"{field_path}.{required} must be a non-empty string")
                if "apiData" not in streaming:
                    raise ValueError(f"{field_path}.apiData is required")
                isrc = streaming.get("isrc")
                if isrc is not None and not isinstance(isrc, str):
                    raise ValueError(f"{field_path}.isrc must be a string or null")
    media_files = document.get("mediaFileData")
    if media_files is not None:
        if not is_list(media_files):
            raise ValueError("mediaFileData must be a list")
        for index, media_file in enumerate(media_files):
            field_path = f"mediaFileData[{index}]"
            if not is_mapping(media_file) or media_file.keys() - MEDIA_FILE_FIELDS:
                raise ValueError(f"{field_path} must contain only supported media-file fields")
            for key, value in media_file.items():
                value_path = f"{field_path}.{key}"
                if key == "fpcalcData":
                    if value is not None:
                        if not is_mapping(value) or value.keys() - FPCALC_FIELDS:
                            raise ValueError(f"{value_path} must be a fingerprint mapping or null")
                        duration = value.get("duration")
                        fingerprint = value.get("fingerprint")
                        if duration is not None and type(duration) not in (int, float):
                            raise ValueError(f"{value_path}.duration must be a number or null")
                        if fingerprint is not None and not isinstance(fingerprint, str):
                            raise ValueError(f"{value_path}.fingerprint must be a string or null")
                    continue
                if key == "acoustidData":
                    if value is not None and not is_mapping(value):
                        raise ValueError(f"{value_path} must be a mapping or null")
                    continue
                expected_type = int if key in {"size", "year", "duration"} else str
                if value is not None and (not isinstance(value, expected_type) or
                                          (expected_type is int and isinstance(value, bool))):
                    raise ValueError(f"{value_path} must be a {expected_type.__name__} or null")
    loaded: object = getattr(TrackYamlFile, "from_yaml")(content)
    if not isinstance(loaded, TrackYamlFile):
        raise ValueError("Expected a TrackYamlFile")
    return document


def has_identifiers(document: Mapping[str, object]) -> bool:
    identifiers = document.get("trackIdData")
    if not is_mapping(identifiers):
        return False
    streaming_data = identifiers.get("streamingData")
    if not is_list(streaming_data):
        return False
    for streaming in streaming_data:
        if not is_mapping(streaming):
            continue
        for key in ("trackId", "trackUrl"):
            value = streaming.get(key)
            if isinstance(value, str) and value.strip():
                return True
    return False


def preserve_existing_values(original: object, completed: object) -> None:
    """Reject a response that removes or changes known source metadata."""
    if is_mapping(original):
        if not is_mapping(completed):
            raise ValueError("Model removed existing metadata")
        for key, value in original.items():
            preserve_existing_values(value, completed.get(key))
    elif is_list(original) and original:
        if not is_list(completed):
            raise ValueError("Model removed existing identifiers")
        for entry in original:
            if is_mapping(entry):
                preserved = False
                for candidate in completed:
                    if not is_mapping(candidate):
                        continue
                    try:
                        preserve_existing_values(entry, candidate)
                    except ValueError:
                        continue
                    preserved = True
                    break
                if not preserved:
                    raise ValueError("Model changed existing track identifiers")
            elif entry not in completed:
                raise ValueError("Model changed existing track identifiers")
    elif (
        original not in (None, "", [])
        and not (isinstance(original, str) and not original.strip())
        and original != completed
    ):
        raise ValueError("Model changed existing track metadata")


def load_input_files(path: Path, album_directories_only: bool = True) -> list[Path]:
    return sorted(
        file
        for file in iter_collection_files(path, album_directories_only)
        if file.suffix.lower() in {".yml", ".yaml"}
        and file.stem.lower() not in {"artist", "album", "files", "artists"}
    )


def show_track_yaml_diff(
    input_path: Path, original: Mapping[str, object], completed: Mapping[str, object]
) -> None:
    """Display changed YAML fields using the merge script's before/after layout."""

    def flatten(value: object, prefix: str) -> dict[str, object]:
        result: dict[str, object] = {}
        if is_mapping(value) and value:
            for key, child in value.items():
                result.update(flatten(child, f"{prefix}.{key}" if prefix else key))
        elif is_list(value) and value:
            for index, child in enumerate(value):
                result.update(flatten(child, f"{prefix}[{index}]"))
        else:
            result[prefix] = value
        return result

    before = flatten(original, "") if original else {}
    after = flatten(completed, "") if completed else {}
    changes: list[str] = []
    for field in sorted(before.keys() | after.keys()):
        if field in before and field in after and before[field] == after[field]:
            continue
        old_value = repr(before[field]) if field in before else "(missing)"
        new_value = repr(after[field]) if field in after else "(missing)"
        changes.append(format_field_diff(field, old_value, new_value, "LLM Result"))
    if changes:
        print(f"\n{input_path}\n" + "\n".join(changes), flush=True)
    else:
        logger.info("[%s] No metadata differences (YAML formatting only)", input_path)


def process_track_yaml_file(
    input_path: Path,
    reference_examples: str,
    file_number: int,
    total_files: int,
    llm_enabled: bool = True,
    client: OpenAI | None = None,
    dry_run: bool = False,
) -> bool:
    logger.info("[%d/%d] Processing %s", file_number, total_files, input_path)
    try:
        raw_yaml = input_path.read_text(encoding="utf-8")
        if not llm_enabled:
            logger.info("[%s] LLM lookup disabled; leaving file unchanged", input_path)
            return True
        if client is None:
            raise ValueError("LLM lookup enabled but no client was provided")
        original = load_document(raw_yaml) if raw_yaml.strip() else {}
        prompt = (
            f"Reference examples:\n{reference_examples}\n\n"
            if reference_examples
            else ""
        ) + f"Fill in this track YAML file (path: {input_path}):\n\n{raw_yaml}"
        logger.info("[%s] Sending request to model %s", input_path, MODEL_NAME)
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        if not response.choices or not response.choices[0].message.content:
            raise ValueError("Model response did not contain YAML content")
        completed_yaml = normalize_model_identifiers(
            response.choices[0].message.content.strip()
        )
        completed = validate_track_yaml_content(completed_yaml)
        preserve_existing_values(original, completed)
        if original.get("mediaFileData") != completed.get("mediaFileData"):
            raise ValueError("Model added, removed, or changed mediaFileData")
        if completed_yaml == raw_yaml.strip():
            logger.info(
                "[%s] Model result unchanged; leaving file untouched", input_path
            )
            return True
        show_track_yaml_diff(input_path, original, completed)
        show_yaml_file_diff(input_path, raw_yaml, completed_yaml + "\n")
        if dry_run:
            proposal_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix="track-yaml-proposal-",
                    suffix=".yml",
                    delete=False,
                ) as stream:
                    proposal_path = Path(stream.name)
                    stream.write(completed_yaml + "\n")
            except OSError:
                if proposal_path is not None:
                    proposal_path.unlink(missing_ok=True)
                raise
            logger.info(
                "[%s] [dry-run] Diff shown; source and backups left unchanged",
                input_path,
            )
            logger.info(
                "Proposal retained for review at %s; remove it when finished",
                proposal_path,
            )
            print(
                shlex.join(["meld", str(input_path.resolve()), str(proposal_path)]),
                flush=True,
            )
            return True

        def write_completed_yaml(stream: TextWriter) -> None:
            stream.write(completed_yaml + "\n")

        backup_path = input_path.with_name(f"{input_path.name}.bak")
        save_yaml_atomically(input_path, write_completed_yaml, backup_path)
        logger.info(
            "[%s] Saved track YAML; original backed up to %s", input_path, backup_path
        )
        print(
            shlex.join(["meld", str(backup_path.resolve()), str(input_path.resolve())]),
            flush=True,
        )
        return True
    except APIStatusError as error:
        logger.error(
            "[%s] Provider returned HTTP %d: %s. Check LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL.",
            input_path,
            error.status_code,
            error,
        )
    except (APIError, *YAML_ERRORS) as error:
        logger.error("[%s] Could not populate track YAML: %s", input_path, error)
    return False


def main_loop(
    sleep_between_files: int,
    llm_enabled: bool = True,
    specific_file: Path | None = None,
    force: bool = False,
    specific_dir: Path | None = None,
    dry_run: bool = False,
    count: int | None = None,
) -> bool:
    if count is not None and count < 0:
        raise ValueError("count must be non-negative")
    if specific_file is not None:
        if not specific_file.is_file():
            raise ValueError(f"Specified file does not exist: {specific_file}")
        input_files = [specific_file]
    else:
        path = collection_data_path(specific_dir)
        input_files = load_input_files(
            path, album_directories_only=specific_dir is None
        )
    pending: list[Path] = []
    examples: list[str] = []
    for input_file in input_files:
        try:
            content = input_file.read_text(encoding="utf-8")
            document = validate_track_yaml_content(content)
        except YAML_ERRORS as error:
            logger.warning("[%s] Track YAML requires processing: %s", input_file, error)
        else:
            if has_identifiers(document):
                if len(examples) < 3:
                    examples.append(content)
                if not force:
                    logger.info("Skipping already-complete track YAML: %s", input_file)
                    continue
        pending.append(input_file)
    if not llm_enabled:
        logger.info(
            "LLM lookup disabled; %d pending file(s), no files changed", len(pending)
        )
        return True
    random.shuffle(pending)
    if count is not None:
        pending = pending[:count]
        logger.info(
            "Processing limit: %d; selected %d pending file(s)", count, len(pending)
        )
    if not pending:
        logger.info("No track YAML files require processing")
        return True
    client = get_client()
    succeeded = True
    for index, input_file in enumerate(
        tqdm(pending, desc="Processing tracks", unit="file"), 1
    ):
        if index > 1:
            time.sleep(sleep_between_files)
        if not process_track_yaml_file(
            input_file,
            "\n\n".join(examples),
            index,
            len(pending),
            client=client,
            dry_run=dry_run,
        ):
            succeeded = False
    return succeeded


def collection_data_path(specific_dir: Path | None = None) -> Path:
    if specific_dir is not None:
        path = specific_dir
    else:
        root = os.environ.get("MOONGAS_COLLECTION_ROOTDIR")
        if not root:
            raise ValueError(
                "MOONGAS_COLLECTION_ROOTDIR must be set, or use --file/--dir"
            )
        path = Path(root) / "data"
    if not path.is_dir():
        raise ValueError(f"Input directory does not exist: {path}")
    return path


def clean(
    specific_file: Path | None = None,
    specific_dir: Path | None = None,
    dry_run: bool = False,
) -> None:
    backups = (
        [specific_file.with_name(f"{specific_file.name}.bak")]
        if specific_file is not None
        else [
            path
            for path in iter_collection_files(
                collection_data_path(specific_dir), album_directories_only=False
            )
            if path.name.lower().endswith((".yml.bak", ".yaml.bak"))
            and Path(path.stem).stem.lower()
            not in {"artist", "album", "files", "artists"}
        ]
    )
    for backup in backups:
        if backup.is_file():
            if dry_run:
                logger.info("[dry-run] Would remove backup file: %s", backup)
                continue
            backup.unlink()
            logger.info("Removed backup file: %s", backup)


def revert(
    specific_file: Path | None = None,
    specific_dir: Path | None = None,
    dry_run: bool = False,
) -> None:
    if specific_file is not None:
        if (
            specific_file.suffix != ".yml"
            or specific_file.stem.lower() in {"artist", "album", "files", "artists"}
            or not AlbumPathBuilder.of(specific_file.parent).valid
        ):
            raise ValueError(
                "--revert --file requires a track .yml file in an album directory"
            )
        backups = [specific_file.with_name(f"{specific_file.name}.bak")]
        if not backups[0].is_file():
            raise ValueError(f"Track YAML backup does not exist: {backups[0]}")
    else:
        backups = [
            path
            for path in iter_collection_files(collection_data_path(specific_dir))
            if path.name.endswith(".yml.bak")
            and Path(path.stem).stem.lower()
            not in {"artist", "album", "files", "artists"}
        ]
    restored = 0
    for backup in backups:
        if backup.is_symlink() or not backup.is_file():
            raise ValueError(
                f"Track YAML backup must be a regular file, not a symlink: {backup}"
            )
        destination = backup.with_suffix("")
        if dry_run:
            logger.info("[dry-run] Would restore %s from %s", destination, backup)
        else:
            # Replace atomically so a failed rename cannot leave the source deleted.
            os.replace(backup, destination)
            logger.info("Restored %s from %s", destination, backup)
        restored += 1
    logger.info(
        "%s %d track YAML backup(s)",
        "Would restore" if dry_run else "Restored",
        restored,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--file", type=Path, help="Process one existing track YAML file."
    )
    scope.add_argument(
        "-d",
        "--dir",
        type=Path,
        help="Recursively scan a directory instead of the collection.",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--clean",
        action="store_true",
        help="Remove track YAML backups in the selected scope and exit.",
    )
    action.add_argument(
        "--revert",
        action="store_true",
        help="Restore track .yml.bak files in album directories and exit; never restore artist.yml or album.yml.",
    )
    parser.add_argument(
        "--sleep", type=int, default=30, help="Seconds between requests (default: 30)."
    )
    parser.add_argument(
        "--llm",
        type=parse_boolean,
        default=True,
        metavar="true|false",
        help="Enable or disable the use of the LLM.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Also process tracks that already have identifiers.",
    )
    parser.add_argument(
        "-c",
        "--count",
        type=int,
        metavar="VALUE",
        help="Process at most VALUE eligible tracks (non-negative; default: unlimited).",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Request model updates without saving; preview backup deletion/restoration with --clean/--revert.",
    )
    args = parser.parse_args()
    if args.sleep < 0:
        parser.error("--sleep must be non-negative")
    if args.count is not None and args.count < 0:
        parser.error("--count must be non-negative")
    logging.basicConfig(
        level=getattr(
            logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO
        ),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if args.clean:
            clean(args.file, args.dir, dry_run=args.dry_run)
            return 0
        if args.revert:
            revert(args.file, args.dir, dry_run=args.dry_run)
            return 0
        return (
            0
            if main_loop(
                args.sleep,
                args.llm,
                args.file,
                args.force,
                args.dir,
                dry_run=args.dry_run,
                count=args.count,
            )
            else 1
        )
    except YAML_ERRORS as error:
        logger.error("%s", error)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
