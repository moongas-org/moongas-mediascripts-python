"""Shared track resolution, lyrics paths, logging, and non-overwriting writes."""

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

MOONGAS_COLLECTION_ROOTDIR = os.environ.get(
    "MOONGAS_COLLECTION_ROOTDIR", "../../moongas-collection-demo"
)

MEDIA_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".flac",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
    ".aac",
    ".mp4",
    ".webm",
    ".mkv",
)
TRACK_YAML_EXTENSIONS = {".yml", ".yaml"}
LYRICS_TEXT_EXTENSION = ".txt"
LYRICS_LRC_EXTENSION = ".lrc"

STATUS_SAVED = "saved"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def lyrics_text_path_for(track_path: Path) -> Path:
    return track_path.with_suffix(LYRICS_TEXT_EXTENSION)


def lyrics_lrc_path_for(track_path: Path) -> Path:
    return track_path.with_suffix(LYRICS_LRC_EXTENSION)


def resolve_track_file(track_path: Path) -> Path:
    """Resolve YAML to sibling media in audio-first preference order, or retain it."""
    if not track_path.is_file():
        raise FileNotFoundError(f"Specified file does not exist: {track_path}")
    if track_path.suffix.lower() in MEDIA_EXTENSIONS:
        return track_path
    if track_path.suffix.lower() not in TRACK_YAML_EXTENSIONS:
        raise ValueError(f"Specified file is not a media track or track YAML file: {track_path}")
    candidates = sorted(
        (
            candidate for candidate in track_path.parent.iterdir()
            if candidate.stem == track_path.stem
            and candidate.suffix.lower() in MEDIA_EXTENSIONS
            and candidate.is_file()
        ),
        key=lambda candidate: (MEDIA_EXTENSIONS.index(candidate.suffix.lower()), candidate.name),
    )
    if candidates:
        logger.info("Using sibling media file for %s: %s", track_path, candidates[0])
        return candidates[0]
    return track_path


def save_lyrics_file(lyrics_path: Path, content: str) -> None:
    """Publish complete UTF-8 lyrics atomically; raise FileExistsError rather than overwrite."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=lyrics_path.parent,
            prefix=f".{lyrics_path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(content)
        os.link(temporary_path, lyrics_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
