"""Shared artist YAML discovery, validation, cleanup, and CLI helpers."""

import argparse
import logging
import os
from pathlib import Path

from dataclass_wizard.v0.errors import MissingFields
from mediascan.types.artist.artist_yaml_file import ArtistYamlFile
from mediascan.types.artist.artist_yaml_file_validator import (
    validate_artist_yaml_content,
)
from mediascan.utils.db.mediascan_db_loader import load_db
from sqlalchemy import text

logger = logging.getLogger(__name__)

EXCLUDE_DIRS = ["Various Artists"]


def excluded(path: Path) -> bool:
    return any(directory in str(path) for directory in EXCLUDE_DIRS)


def print_artist_yaml_summary(total: int, missing: int, missing_label: str) -> None:
    print(f"Found artist.yml for {total - missing} out of {total} artist folders")
    print(f"{missing_label}: {missing}")


def load_artist_paths_from_database(parser: argparse.ArgumentParser) -> dict[str, Path]:
    collection_root = os.environ.get("MOONGAS_COLLECTION_ROOTDIR")
    if not collection_root:
        parser.error("MOONGAS_COLLECTION_ROOTDIR must be set to the active collection directory")
    database_path = (Path(collection_root) / "mediascan.db").resolve()
    if not database_path.is_file():
        parser.error(f"Moongas database file not found: {database_path}")

    database_url = f"sqlite:///{Path(os.path.relpath(database_path)).as_posix()}"
    engine = load_db(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT name, path FROM artist ORDER BY name")).mappings()
            artist_paths: dict[str, Path] = {}
            for row in rows:
                artist = row["name"]
                path = row["path"]
                if not isinstance(artist, str) or not isinstance(path, str):
                    raise ValueError(f"Invalid artist record in {database_path}: name and path must be strings")
                artist_path = Path(path)
                if not artist_path.is_absolute():
                    artist_path = Path(collection_root) / artist_path
                artist_paths[artist] = artist_path
    finally:
        engine.dispose()
    return artist_paths


def load_reference_examples(collection_data_path: Path, letters_dirs_to_work: list[str]) -> str:
    """Load complete YAML files that demonstrate the desired output format.
    Currently it recursively loads all artist.yaml files under the EXAMPLES_ROOTDIR
    Only files named "artist.yml" in directories starting with "A" are considered.
    This avoids maximum context length issues by limiting the number of examples loaded.
    Also the ones starting with the letter 'A' are considered the primary examples
    because I tagged them semi-manually using Copilot Chat first.
    """
    logger.info(
        "Loading reference examples from directories starting with letters: %s",
        letters_dirs_to_work,
    )

    if not collection_data_path.is_dir():
        logger.warning("Reference examples directory not found: %s", collection_data_path)
        return ""
    else:
        logger.info("Reference examples directory found: %s", collection_data_path)

    example_paths = load_input_files(collection_data_path, letters_dirs_to_work)

    logger.info("Found %d reference example file(s)", len(example_paths))

    examples: list[str] = []
    for example_path in sorted(example_paths):
        filename = os.path.relpath(example_path, collection_data_path)
        with open(example_path, "r", encoding="utf-8") as f:
            examples.append(f"Example: {filename}\n{f.read()}")
    logger.info(
        "Loaded reference example file(s): %s",
        ", ".join(sorted(os.path.relpath(p, collection_data_path) for p in example_paths)),
    )

    logger.info(
        "Loaded %d reference example(s) from %s",
        len(examples),
        collection_data_path,
    )
    reference_text = "\n\n".join(examples)
    logger.info("Reference example content size: %d characters", len(reference_text))
    return reference_text


def load_input_files(collection_data_path: Path, letters_dirs_to_work: list[str]) -> list[Path]:
    """Find artist.yml files in directories selected by their first letter."""
    input_paths = [
        Path(root) / filename
        for root, _, filenames in os.walk(collection_data_path, followlinks=True)
        for filename in filenames
        if filename == "artist.yml" and os.path.basename(root) and os.path.basename(root)[0].upper() in letters_dirs_to_work
    ]

    input_paths = sorted(input_paths)
    logger.info("Found %d YAML input file(s)", len(input_paths))
    for input_path in input_paths:
        logger.debug("Queued input file: %s", input_path)
    return input_paths


def clean_backup_files(collection_data_path: Path, continue_on_error: bool = False) -> int:
    """Delete all backup files beneath the collection root directory."""
    deleted_count = 0
    for backup_path in collection_data_path.rglob("*.bak"):
        if not continue_on_error and not backup_path.is_file():
            continue
        try:
            backup_path.unlink()
        except OSError as exception:
            if not continue_on_error:
                raise
            logger.warning("Failed to remove backup file %s: %s", backup_path, exception)
            continue
        deleted_count += 1
        logger.info("Deleted backup file: %s", backup_path)

    logger.info("Deleted %d backup file(s) beneath %s", deleted_count, collection_data_path)
    return deleted_count


def try_load_artist_yaml_for_skip(input_path: Path) -> bool:
    """Return whether strict loading succeeds before deciding to skip a file."""
    try:
        getattr(ArtistYamlFile, "from_yaml")(input_path.read_text(encoding="utf-8"))
        return True
    except MissingFields as exception:
        logger.warning(
            "Artist YAML requires LLM processing: %s is missing fields %s; rescued raw data: %s",
            input_path,
            exception.missing_fields,
            exception.obj,
        )
    except Exception as exception:
        logger.warning(
            "Artist YAML requires LLM processing: could not strictly load %s: %s",
            input_path,
            exception,
        )
    return False


def validate_llm_yaml_content(content: str, filename: str) -> None:
    """Validate LLM YAML syntax while allowing incomplete schema fields to be saved."""
    try:
        validate_artist_yaml_content(content)
    except ValueError as exception:
        logger.warning(
            "[%s] LLM YAML has validation warnings; saving response for further processing: %s",
            filename,
            exception,
        )
    else:
        logger.info("[%s] YAML validation passed", filename)
