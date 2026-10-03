import re
import logging
import os
from pathlib import Path
from dataclass_wizard.v0.errors import MissingFields
from mediascan.artist_yaml_file import ArtistYamlFile
from mediascan.artist_yaml_file_validator import (
    validate_artist_yaml_content,
)

# Configure with LOG_LEVEL=DEBUG for additional diagnostic detail.
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_reference_examples(
    collection_data_path: Path, letters_dirs_to_work: list[str]
) -> str:
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
        logger.warning(
            "Reference examples directory not found: %s", collection_data_path
        )
        return ""
    else:
        logger.info("Reference examples directory found: %s", collection_data_path)

    example_paths: list[str] = []

    for root, _, filenames in os.walk(collection_data_path, followlinks=True):
        logger.info(
            "Contents of collection_data_path: %s", os.listdir(collection_data_path)
        )

        base_name = os.path.basename(root)
        logger.info("Checking root: %s, base_name: %s", root, base_name)

        # Ensure the directory name is not empty and starts with a target letter
        if base_name and base_name[0].upper() in letters_dirs_to_work:
            for filename in filenames:
                if filename == "artist.yml":
                    example_paths.append(str(Path(root) / filename))
                    logger.info("Added example file: %s", str(Path(root) / filename))

    logger.info("Found %d reference example file(s)", len(example_paths))

    examples: list[str] = []
    for example_path in sorted(example_paths):
        filename = os.path.relpath(example_path, collection_data_path)
        with open(example_path, "r", encoding="utf-8") as f:
            examples.append(f"Example: {filename}\n{f.read()}")
    logger.info(
        "Loaded reference example file(s): %s",
        ", ".join(
            sorted(os.path.relpath(p, collection_data_path) for p in example_paths)
        ),
    )

    logger.info(
        "Loaded %d reference example(s) from %s",
        len(examples),
        collection_data_path,
    )
    reference_text = "\n\n".join(examples)
    logger.info("Reference example content size: %d characters", len(reference_text))
    return reference_text


def load_input_files(
    collection_data_path: Path, letters_dirs_to_work: list[str]
) -> list[Path]:
    """Find artist.yml files in directories selected by their first letter."""
    input_paths = [
        Path(root) / filename
        for root, _, filenames in os.walk(collection_data_path, followlinks=True)
        for filename in filenames
        if filename == "artist.yml"
        and os.path.basename(root)[0].upper() in letters_dirs_to_work
    ]

    input_paths = sorted(input_paths)
    logger.info("Found %d YAML input file(s)", len(input_paths))
    for input_path in input_paths:
        logger.debug("Queued input file: %s", input_path)
    return input_paths


def clean_backup_files(collection_data_path: Path) -> int:
    """Delete all backup files beneath the collection root directory."""
    deleted_count = 0
    for backup_path in collection_data_path.rglob("*.bak"):
        if not backup_path.is_file():
            continue
        backup_path.unlink()
        deleted_count += 1
        logger.info("Deleted backup file: %s", backup_path)

    logger.info(
        "Deleted %d backup file(s) beneath %s", deleted_count, collection_data_path
    )
    return deleted_count


def try_load_artist_yaml_for_skip(input_path: Path) -> bool:
    """Return whether strict loading succeeds before deciding to skip a file."""
    try:
        getattr(ArtistYamlFile, "from_yaml")(input_path.read_text(encoding="utf-8"))
        return True
    except MissingFields as exception:
        logger.warning(
            "Artist YAML requires LLM processing: %s is missing fields %s; "
            "rescued raw data: %s",
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


def remove_duplicate_members_lists(content: str) -> str:
    """Merge duplicate direct ``members`` lists under ``artistData``."""
    artist_data_match = re.search(
        r"^(?P<indent>[ \t]*)artistData:[ \t]*$", content, re.MULTILINE
    )
    if artist_data_match is None:
        return content

    artist_data_indent = artist_data_match.group("indent")
    first_member_match = re.search(
        rf"^(?P<indent>{re.escape(artist_data_indent)}[ \t]+)members:(?:[ \t]+[^#]*)?[ \t]*$",
        content[artist_data_match.end() :],
        re.MULTILINE,
    )
    if first_member_match is None:
        return content

    member_indent = first_member_match.group("indent")
    member_key = re.compile(
        rf"^{re.escape(member_indent)}members:(?:[ \t]+[^#]*)?[ \t]*$"
    )
    lines = content.splitlines(keepends=True)
    artist_data_line = content[: artist_data_match.start()].count("\n")
    found_members = False
    cleaned_lines: list[str] = []
    line_number = 0

    while line_number < len(lines):
        line = lines[line_number]
        if line_number <= artist_data_line or not member_key.match(line):
            cleaned_lines.append(line)
            line_number += 1
            continue

        if not found_members:
            found_members = True
            cleaned_lines.append(line)
            line_number += 1
            continue

        line_number += 1

    return "".join(cleaned_lines)


def remove_duplicate_members_lists_from_collection(collection_data_path: Path) -> int:
    """Remove duplicate ``members`` lists from every artist YAML under a root."""
    artist_yaml_paths = sorted(collection_data_path.rglob("artist.yml"))
    logger.info(
        "Scanning %d artist YAML file(s) under %s for duplicate members lists",
        len(artist_yaml_paths),
        collection_data_path,
    )
    cleaned_count = 0
    for artist_yaml_path in artist_yaml_paths:
        logger.debug("Checking %s for duplicate members lists", artist_yaml_path)
        raw_yaml = artist_yaml_path.read_text(encoding="utf-8")
        normalized_yaml = remove_duplicate_members_lists(raw_yaml)
        if normalized_yaml == raw_yaml:
            logger.debug("No duplicate members lists found in %s", artist_yaml_path)
            continue
        artist_yaml_path.write_text(normalized_yaml, encoding="utf-8")
        cleaned_count += 1
        logger.info(
            "Merged duplicate members lists and overwrote %s (%d -> %d characters)",
            artist_yaml_path,
            len(raw_yaml),
            len(normalized_yaml),
        )
    logger.info(
        "Completed duplicate members scan: %d modified, %d unchanged",
        cleaned_count,
        len(artist_yaml_paths) - cleaned_count,
    )
    return cleaned_count
