"""Shared helpers for populating sibling lyrics text files."""

import logging
from collections.abc import Iterable
from pathlib import Path

from mediascripts.lyrics.lyrics_common import (
    MEDIA_EXTENSIONS,
    lyrics_text_path_for,
    save_lyrics_file,
)

logger = logging.getLogger(__name__)


def has_lyrics_text(track_path: Path) -> bool:
    lyrics_path = lyrics_text_path_for(track_path)
    return lyrics_path.exists() or lyrics_path.is_symlink()


def is_eligible(track_path: Path) -> bool:
    return (
        track_path.is_file()
        and track_path.suffix.lower() in MEDIA_EXTENSIONS
        and not has_lyrics_text(track_path)
    )


def find_tracks_missing_lyrics(candidates: Iterable[Path]) -> list[Path]:
    tracks = sorted(track for track in candidates if is_eligible(track))
    logger.info("Found %d track(s) missing lyrics text files", len(tracks))
    for track_path in tracks:
        logger.debug("Missing lyrics text file for: %s", track_path)
    return tracks


def log_missing_lyrics_files(track_paths: Iterable[Path]) -> None:
    for track_path in track_paths:
        logger.info("Missing: %s", lyrics_text_path_for(track_path))


def save_lyrics_file_if_missing(lyrics_path: Path, content: str) -> bool:
    try:
        save_lyrics_file(lyrics_path, content)
    except FileExistsError:
        return False
    return True
