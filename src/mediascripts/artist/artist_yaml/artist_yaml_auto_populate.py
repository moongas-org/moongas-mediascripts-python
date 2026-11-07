"""
Usage: python src/mediascripts/artist/artist_yaml/artist_yaml_auto_populate.py [options]
Or from script symlink: artist-yaml-auto-populate [options]

It expects the environment variable MOONGAS_COLLECTION_ROOTDIR to be set,
pointing to the root directory of the Moongas collection,
i.e. the directory containing the `data` subdirectory,
which in turn contains one or more library directories e.g. `Music`

E.g.
user@host:~/Git/moongas-org/moongas/moongas-collection-demo$ ./show-env
------------------------------------------------------------------------------
                             Moongas Environment
------------------------------------------------------------------------------
  MOONGAS_COLLECTION_ROOTDIR:
    "/home/user/Git/moongas-org/moongas/moongas-collection-local"
  MOONGAS_REMOTE_SERVER_IP:
    "1.1.1.1"


./artist-yaml-auto-populate

./artist-yaml-auto-populate --clean

./artist-yaml-auto-populate --file "$MOONGAS_COLLECTION_ROOTDIR/data/Music/Quavo and Takeoff (Unc and Phew)/artist.yml" --force


Command line options:
  --clean    Remove all backup files before processing.
  --sleep    Pause execution for a short period before processing.
  --help     Show this help message and exit.
  --file     Specify a single artist.yml file to process. Must be the full path to the file.
  --dir      Process one artist directory, creating artist.yml from local clues if missing.
  --force    Force processing of all files, ignoring the pre-pass duplicate check and validation check.
"""

import argparse
import logging
import os
import random
import string
import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Optional, Protocol, TextIO, cast

import mutagen
from openai import APIStatusError, OpenAI
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from tqdm import tqdm

from mediascan.types.artist.artist_yaml_file_validator import (
    validate_artist_yaml_file,
)
from mediascripts.artist.artist_yaml.artist_yaml_common import (
    clean_backup_files,
    load_input_files,
    load_reference_examples,
    try_load_artist_yaml_for_skip,
    validate_llm_yaml_content,
)
from mediascripts.common.cli.parse_boolean import parse_boolean
from mediascripts.common.openai.client import BASE_URL, MODEL_NAME, get_client
from mediascripts.lyrics.lyrics_common import MEDIA_EXTENSIONS
from mediascripts.mediafile.auto_tag_media_file import load_media, text_values, unescape_name
from mediascripts.track.track_yaml.track_yaml_common import TextWriter

# Configure with LOG_LEVEL=DEBUG for additional diagnostic detail.
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

LETTER_DIRS_FOR_REFERENCE_EXAMPLES = ["A"]

LETTER_DIRS_NEEDING_WORK = list(string.ascii_uppercase[1:])

assert not (
    set(LETTER_DIRS_FOR_REFERENCE_EXAMPLES) & set(LETTER_DIRS_NEEDING_WORK)
), "Reference examples and needing work arrays intersect!"


# Train on all existing artist.yaml files.
MOONGAS_COLLECTION_ROOTDIR = os.environ.get(
    "MOONGAS_COLLECTION_ROOTDIR", "../../moongas-collection-demo"
)

# Define strict system instructions to prevent conversational text in the response
SYSTEM_PROMPT = """You are an automated data-filling assistant. 
Your job is to read the provided incomplete YAML file and fill in the missing values or inferred fields.
Maintain the exact schema. Return ONLY the valid YAML content. 
Do not include markdown code blocks (like ```yaml), explanations, or opening/closing text.
Try to find the dob and dod (date of birth and date of death) fields if they are missing.
Use only fields supported by ArtistData: artistNames, city, countryCode,
regionCode, languageCodes, and optional dob/dod dates. Dates use y and optional
m/d integer fields. Preserve existing non-empty values and omit facts that cannot
be established.
"""


class _YamlIO(Protocol):
    def load(self, stream: TextIO) -> object: ...
    def dump(self, data: Mapping[str, object], stream: TextWriter) -> None: ...


def infer_artist_name(artist_dir: Path) -> str:
    """Prefer track YAML over media tags across immediate album subdirectories."""
    album_files = [
        path for directory in sorted(artist_dir.iterdir()) if directory.is_dir()
        for path in sorted(directory.iterdir()) if path.is_file()
    ]
    for source in ("YAML", "media"):
        names: list[str] = []
        for path in album_files:
            try:
                if source == "YAML":
                    if path.suffix.lower() not in {".yml", ".yaml"} or path.stem.lower() in {
                        "album", "artist", "files", "artists",
                    }:
                        continue
                    with path.open(encoding="utf-8") as stream:
                        document = cast(_YamlIO, YAML(typ="safe")).load(stream)
                    if not isinstance(document, dict):
                        raise ValueError("Expected a YAML mapping")
                    document_mapping = cast(Mapping[str, object], document)
                    data = document_mapping.get("trackData", document_mapping.get("track_data"))
                    if data is None:
                        continue
                    if not isinstance(data, dict):
                        raise ValueError("Expected a trackData mapping")
                    metadata = cast(Mapping[str, object], data)
                    candidates = [metadata.get("albumartist"), metadata.get("artist")]
                    for candidate in candidates:
                        if candidate is not None and not isinstance(candidate, str):
                            raise ValueError("Track artist names must be strings")
                    name = next(
                        (candidate.strip() for candidate in candidates if isinstance(candidate, str) and candidate.strip()),
                        "",
                    )
                else:
                    if path.suffix.lower() not in MEDIA_EXTENSIONS:
                        continue
                    media = load_media(path)
                    candidates = [value for value in text_values(media, "albumartist") if value.strip()]
                    if not candidates:
                        candidates = text_values(media, "artist")
                    name = next((candidate.strip() for candidate in candidates if candidate.strip()), "")
                if name:
                    names.append(name)
            except (OSError, UnicodeError, ValueError, TypeError, YAMLError, mutagen.MutagenError) as error:
                logger.warning("Could not read artist clues from %s: %s", path, error)
        if names:
            counts = Counter(names)
            name = counts.most_common(1)[0][0]
            if len(counts) > 1:
                logger.warning("Conflicting %s artist names in %s: %s; selecting %r", source, artist_dir, dict(counts), name)
            logger.info("Using %s artist name %r for %s", source, name, artist_dir)
            return name
    name = unescape_name(artist_dir.name)
    if not name:
        raise ValueError(f"Cannot infer artist name from {artist_dir}")
    logger.info("No track artist clues found; using directory name %r", name)
    return name


def ensure_artist_yaml(artist_dir: Path, overrides: Mapping[str, object]) -> tuple[Path, bool]:
    """Create a minimal schema without overwriting an existing artist file."""
    if not artist_dir.is_dir():
        raise NotADirectoryError(f"Artist directory does not exist or is not a directory: {artist_dir}")
    path = artist_dir / "artist.yml"
    if path.exists():
        if not path.is_file():
            raise ValueError(f"Artist YAML path is not a file: {path}")
        logger.info("Using existing artist YAML: %s", path)
        return path, False
    data: dict[str, object] = {
        "artistNames": overrides.get("artistNames") or [infer_artist_name(artist_dir)],
        "city": "", "countryCode": "", "regionCode": "", "languageCodes": [],
    }
    data.update(overrides)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=artist_dir,
            prefix=".artist.yml.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            cast(_YamlIO, YAML()).dump({"artistData": data}, stream)
        os.link(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    logger.info("Created minimal artist YAML from local clues: %s", path)
    return path, True


def process_artist_yaml_file(
    input_path: Path,
    reference_examples: str,
    file_number: int,
    total_files: int,
    llm_enabled: bool = True,
    client: Optional[OpenAI] = None,
) -> bool:
    filename = input_path.name
    started_at = time.monotonic()
    temporary_path: Path | None = None

    logger.info("[%d/%d] Processing %s", file_number, total_files, input_path)

    try:
        with open(input_path, "r", encoding="utf-8") as f:
            raw_yaml = f.read()
        logger.debug("Read %d characters from %s", len(raw_yaml), input_path)
        logger.info("[%s] YAML before modification:\n%s", filename, raw_yaml)

        if not llm_enabled:
            logger.info("[%s] LLM lookup disabled; leaving file unchanged", filename)
            return True

        if not client:
            logger.error(
                "[%s] LLM lookup enabled; but no client was provided", filename
            )
            return False

        # Call the LLM to fill in the info
        prompt = (
            "Use these completed YAML files as reference examples.\n\n"
            f"{reference_examples}\n\n"
            "Fill in this YAML file:\n\n"
            f"{raw_yaml}"
            if reference_examples
            else f"Fill in this YAML file:\n\n{raw_yaml}"
        )
        logger.info(
            "[%s] Sending request to model %s (%d prompt characters)",
            filename,
            MODEL_NAME,
            len(prompt),
        )
        response = client.chat.completions.create(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=MODEL_NAME,
            temperature=0.2,
        )
        logger.info("[%s] Received model response", filename)

        content = response.choices[0].message.content
        if content is None:
            raise ValueError("Model response did not contain YAML content")
        completed_yaml = content.strip()
        if not completed_yaml:
            raise ValueError("Model response contained empty YAML content")
        logger.info(
            "[%s] Model response contains %d characters", filename, len(completed_yaml)
        )

        validate_llm_yaml_content(completed_yaml, filename)
        logger.info("[%s] YAML after modification:\n%s", filename, completed_yaml)

        backup_path = input_path.with_name(f"{input_path.name}.bak")
        if completed_yaml == raw_yaml.strip():
            logger.info(
                "[%s] LLM result is unchanged; leaving source file untouched", filename
            )
            if backup_path.exists():
                backup_path.unlink()
                logger.info("[%s] Removed stale backup file %s", filename, backup_path)
            return True

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=input_path.parent,
            prefix=f".{input_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(completed_yaml)
            temporary_path = Path(temporary_file.name)
        logger.debug("[%s] Staged generated YAML at %s", filename, temporary_path)

        shutil.copy2(input_path, backup_path)
        logger.info("[%s] Backed up original file to %s", filename, backup_path)

        os.replace(temporary_path, input_path)
        temporary_path = None
        logger.debug("[%s] Wrote generated YAML to %s", filename, input_path)

        duration = time.monotonic() - started_at
        logger.info(
            "[%s] Saved %d characters to %s in %.2f seconds",
            filename,
            len(completed_yaml),
            input_path,
            duration,
        )
        logger.info(
            "Diff tool command to compare the original YAML with the generated YAML: \n\n"
            'meld "%s" "%s"\n\n',
            backup_path,
            input_path,
        )
        logger.info("[%s] Processing completed successfully", filename)
        return True

    except APIStatusError as e:
        duration = time.monotonic() - started_at
        if e.status_code == 410:
            logger.error(
                "[%s] Provider returned HTTP 410 after %.2f seconds: "
                "the service is unavailable or retired. Set LLM_BASE_URL, "
                "LLM_API_KEY, and LLM_MODEL to use another provider.",
                filename,
                duration,
            )
        else:
            logger.exception(
                "[%s] API request failed after %.2f seconds (HTTP %s): %s",
                filename,
                duration,
                e.status_code,
                e,
            )
    except Exception as e:
        duration = time.monotonic() - started_at
        logger.exception(
            "[%s] Failed after %.2f seconds (%s): %s",
            filename,
            duration,
            type(e).__name__,
            e,
        )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
            logger.debug("[%s] Removed staged temporary file", filename)
    return False


def process_artist_yaml_files(
    path: Path,
    letters_dirs_to_work: list[str],
    processed_files: set[Path],
    llm_enabled: bool = True,
    specific_file: Optional[Path] = None,
    force: bool = False,
) -> bool | None:
    logger.info("Starting YAML processing")
    logger.info("Input directory: %s", path.resolve())
    logger.info("Output directory: %s", path.resolve())
    logger.info("API endpoint: %s", BASE_URL)
    logger.info("Model: %s", MODEL_NAME)
    client = get_client()
    logger.info("Client: %s", client)

    if specific_file is not None:
        if not specific_file.exists():
            logger.error("Specified file does not exist: %s", specific_file)
            return False
        else:
            logger.info("Processing specified file: %s", specific_file)

    if not path.is_dir():
        logger.error("Input directory does not exist: %s", path.resolve())
        return False

    input_files: list[Path] = []
    if specific_file is not None:
        input_files = [specific_file]
    else:
        input_files = [
            input_file
            for input_file in load_input_files(path, letters_dirs_to_work)
            if input_file not in processed_files
        ]
    if not input_files:
        logger.warning(
            "No unprocessed input files remain for letter directories: %s",
            letters_dirs_to_work,
        )
        return None

    for input_file in input_files:
        if not try_load_artist_yaml_for_skip(input_file):
            continue
        artists_missing: list[str] = []
        exceptions: list[tuple[Path, Exception]] = []
        validate_artist_yaml_file(
            input_file.parent.name,
            input_file.parent,
            [str(input_file)],
            artists_missing,
            exceptions,
        )
        if not artists_missing and not exceptions and not force:
            processed_files.add(input_file)
            logger.info("Skipping already-valid artist YAML file: %s", input_file)
        else:
            if artists_missing:
                logger.warning(
                    "Artist YAML validation failed for %s: artist.yml is missing",
                    input_file,
                )
            for exception_path, exception in exceptions:
                logger.warning(
                    "Artist YAML validation failed for %s: %s",
                    exception_path,
                    exception,
                )

    input_files = [
        input_file for input_file in input_files if input_file not in processed_files
    ]
    if not input_files:
        logger.info("All matching files are already valid; exiting loop")
        return None

    reference_examples = load_reference_examples(
        Path(MOONGAS_COLLECTION_ROOTDIR) / "data", LETTER_DIRS_FOR_REFERENCE_EXAMPLES
    )

    logger.info(
        "Selecting 1 random file from %d available input file(s) with %d reference characters",
        len(input_files),
        len(reference_examples),
    )
    selected_file = random.choice(input_files)
    processed_files.add(selected_file)
    logger.info("Selected random input file: %s", selected_file)
    results = [
        process_artist_yaml_file(
            selected_file,
            reference_examples,
            1,
            1,
            llm_enabled=llm_enabled,
            client=client,
        )
    ]
    succeeded = sum(results)
    failed = len(results) - succeeded
    logger.info("Batch complete: %d succeeded, %d failed", succeeded, failed)
    return succeeded > 0 and failed == 0


def clean():
    collection_data_path = Path(MOONGAS_COLLECTION_ROOTDIR) / "data"
    logger.info(
        "Cleaning up backup files in the collection: %s",
        collection_data_path,
    )
    count_removed = clean_backup_files(collection_data_path, continue_on_error=True)
    logger.info("Removed a total of %d backup file(s)", count_removed)


def main_loop(
    sleep_between_files: int,
    llm_enabled: bool = True,
    specific_file: Optional[Path] = None,
    force: bool = False,
):
    processed_files: set[Path] = set()
    letter_dirs = LETTER_DIRS_NEEDING_WORK
    collection_data_path = Path(MOONGAS_COLLECTION_ROOTDIR) / "data"
    if not llm_enabled:
        logger.info("LLM lookup disabled; exiting without modifying artist YAML files")
        return
    if specific_file:
        specific_file = Path(specific_file)
        collection_data_path = specific_file.parent
        total_files = 1
    else:
        total_files = len(load_input_files(collection_data_path, letter_dirs))
    with tqdm(total=total_files, desc="Processing files", unit="file") as progress:
        while True:
            processed_count = len(processed_files)
            logger.info(
                "Starting processing loop for letter directories: %s", letter_dirs
            )
            result = process_artist_yaml_files(
                collection_data_path,
                letter_dirs,
                processed_files,
                llm_enabled=llm_enabled,
                specific_file=specific_file,
                force=force,
            )
            progress.update(len(processed_files) - processed_count)
            if result is None:
                logger.info("All matching files have been processed; exiting loop")
                return
            logger.info(
                "Processing loop complete; sleeping for %d seconds",
                sleep_between_files,
            )
            if len(processed_files) >= total_files:
                logger.info("Reached total number of files to process; exiting loop")
                return
            for _ in tqdm(
                range(sleep_between_files),
                desc="Sleeping",
                bar_format="{desc}: |{bar}| {remaining} remaining",
                leave=False,
            ):
                time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Process artist YAML files or clean up backup files."
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean backup files and exit instead of starting the processing loop.",
    )
    parser.add_argument(
        "--sleep",
        type=int,
        default=30,
        help="Seconds to sleep between processing loops (default: 30).",
    )
    parser.add_argument(
        "--llm",
        type=parse_boolean,
        default=True,
        metavar="true|false",
        help="Enable LLM lookups (default: true). Use --llm=false for pre-pass only.",
    )
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument(
        "--file",
        type=str,
        default=None,
        help="Process a single specific artist YAML file instead of scanning all directories.",
    )
    targets.add_argument(
        "--dir", type=Path,
        help="Process an artist directory; create missing artist.yml from CLI, track YAML, media tags, or directory name.",
    )
    parser.add_argument("--artist", action="append", help="Artist name for a new artist.yml; repeat for aliases.")
    parser.add_argument("--city", help="City for a new artist.yml.")
    parser.add_argument("--country-code", help="Country code for a new artist.yml.")
    parser.add_argument("--region-code", help="Region code for a new artist.yml.")
    parser.add_argument("--language-code", action="append", help="Language code for a new artist.yml; repeat for languages.")
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Force processing of all files, ignoring the pre-pass duplicate check and validation check.",
    )
    args = parser.parse_args(argv)
    seed_options: dict[str, str | list[str] | None] = {
        "artistNames": args.artist, "city": args.city, "countryCode": args.country_code,
        "regionCode": args.region_code, "languageCodes": args.language_code,
    }
    overrides = {key: value for key, value in seed_options.items() if value is not None}
    if overrides and args.dir is None:
        parser.error("Artist metadata options require --dir")
    for key, value in overrides.items():
        entries = value if isinstance(value, list) else [value]
        if any(not entry.strip() for entry in entries):
            parser.error(f"{key} must not be empty")
    if args.clean and args.dir is not None:
        parser.error("--clean cannot be combined with --dir")

    if args.clean:
        clean()
    else:
        try:
            if args.dir is not None:
                specific_file, created = ensure_artist_yaml(args.dir, overrides)
                args.file = specific_file
                args.force = args.force or created
            main_loop(
                sleep_between_files=args.sleep,
                llm_enabled=args.llm,
                specific_file=args.file,
                force=args.force,
            )
        except KeyboardInterrupt:
            logger.info("Interrupted by user; exiting")
            return 130
        except (OSError, ValueError, TypeError, YAMLError) as error:
            logger.error("Could not process artist directory: %s", error)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
