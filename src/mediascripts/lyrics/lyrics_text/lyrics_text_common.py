"""Shared helpers for populating sibling lyrics text files."""

import logging
import os
from collections.abc import Iterable
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

import requests

from mediascripts.lyrics.lyrics_common import (
    MEDIA_EXTENSIONS,
    lyrics_text_path_for,
    save_lyrics_file,
)

logger = logging.getLogger(__name__)


class TranscribedSegment(Protocol):
    @property
    def start(self) -> float: ...

    @property
    def text(self) -> str: ...


class TranscriptionModel(Protocol):
    def transcribe(
        self, audio: str, *, language: str,
    ) -> tuple[Iterable[TranscribedSegment], object]: ...


class FasterWhisper(Protocol):
    def WhisperModel(
        self, model_size_or_path: str, *, device: str, compute_type: str,
    ) -> TranscriptionModel: ...


def log_model_file_size(model_name: str, faster_whisper: FasterWhisper) -> None:
    model_path = Path(model_name)
    if model_path.is_dir():
        weights_path = model_path / "model.bin"
        try:
            size = weights_path.stat().st_size
        except OSError as error:
            logger.warning("Could not check model file size for %s: %s", weights_path, error)
            return
        logger.info("Local model file %s size: %.2f MiB", weights_path, size / (1024 * 1024))
        return

    # Use faster-whisper's own mapping: aliases such as "large" are not repository names.
    utils: object = getattr(faster_whisper, "utils", None)
    models: object = getattr(utils, "_MODELS", {})
    repo_id: object = cast(dict[object, object], models).get(model_name) if isinstance(models, dict) else None
    if repo_id is None and len(model_name.split("/")) == 2:
        repo_id = model_name
    if not isinstance(repo_id, str):
        logger.info("Could not determine model repository for %s; file size unavailable", model_name)
        return
    logger.info("Checking model repository: %s", repo_id)
    if os.environ.get("HF_HUB_OFFLINE", "").upper() in {"1", "ON", "YES", "TRUE"}:
        logger.info("Hugging Face offline mode enabled; remote model file size unavailable")
        return

    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    try:
        response = requests.head(f"{endpoint}/{repo_id}/resolve/main/model.bin", timeout=5)
        response.raise_for_status()
        size_header = response.headers.get("X-Linked-Size")
        if size_header is None and response.status_code == 200:
            size_header = response.headers.get("Content-Length")
        if size_header is None:
            logger.info("Model file size unavailable for %s/model.bin", repo_id)
            return
        size = int(size_header)
        if size < 0:
            raise ValueError("negative model file size")
        logger.info("Model file %s/model.bin size: %.2f MiB", repo_id, size / (1024 * 1024))
    except (requests.RequestException, ValueError) as error:
        logger.warning("Could not check remote model file size for %s: %s", repo_id, error)


def load_transcription_model(model_name: str, device: str | None) -> TranscriptionModel:
    try:
        faster_whisper = cast(FasterWhisper, import_module("faster_whisper"))
    except ImportError:
        logger.exception("Could not import faster_whisper; install with pip install 'mediascripts[lyrics]'")
        raise
    try:
        log_model_file_size(model_name, faster_whisper)
        logger.info("Loading faster-whisper model %s (device=%s, compute_type=int8)", model_name, device or "auto")
        model = faster_whisper.WhisperModel(
            model_name, device=device or "auto", compute_type="int8",
        )
    except (OSError, RuntimeError, ValueError):
        logger.exception("Could not load faster-whisper model %s", model_name)
        raise
    logger.info("faster-whisper model %s loaded successfully", model_name)
    return model


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
