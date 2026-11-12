"""Populate missing sibling lyrics text files using LRCLIB, then local audio."""

import argparse
import logging
import random
from collections import Counter, deque
from datetime import date
from importlib import import_module
from pathlib import Path
from typing import cast

from tqdm import tqdm

from mediascripts.common.cli.parse_boolean import parse_boolean
from mediascripts.lyrics.lyrics_common import (
    MEDIA_EXTENSIONS,
    MOONGAS_COLLECTION_ROOTDIR,
    STATUS_ERROR,
    STATUS_SAVED,
    STATUS_SKIPPED,
    configure_logging,
    resolve_track_file,
)
from mediascripts.lyrics.lyrics_text import (
    lyrics_text_auto_populate_from_file as media_transcription,
)
from mediascripts.lyrics.lyrics_text import (
    lyrics_text_auto_populate_from_lrclib as lrclib,
)
from mediascripts.lyrics.lyrics_text.lyrics_text_common import (
    has_lyrics_text,
    log_missing_lyrics_files,
)

configure_logging()
logger = logging.getLogger(__name__)

STATUS_INSTRUMENTAL = lrclib.STATUS_INSTRUMENTAL
STATUS_NOT_FOUND = lrclib.STATUS_NOT_FOUND
STATUS_RETRY = lrclib.STATUS_RETRY


def _find_tracks(
    specific_file: Path | None, specific_dir: Path | None,
) -> list[Path] | None:
    if specific_file is not None:
        try:
            track_path = resolve_track_file(specific_file)
        except (OSError, ValueError) as error:
            logger.error("Could not select track: %s", error)
            return None
        if (
            track_path.suffix.lower() not in MEDIA_EXTENSIONS
            and track_path.suffix.lower() not in lrclib.TRACK_YAML_EXTENSIONS
        ):
            logger.error("Specified file is not a media track or track YAML file: %s", track_path)
            return None
        return [] if has_lyrics_text(track_path) else [track_path]

    if specific_dir is not None:
        if not specific_dir.is_dir():
            logger.error("Specified directory does not exist: %s", specific_dir)
            return None
        return lrclib.find_tracks_missing_lyrics_in_directory(specific_dir)

    collection_data_path = Path(MOONGAS_COLLECTION_ROOTDIR) / "data"
    logger.info("Collection data directory: %s", collection_data_path.resolve())
    if not collection_data_path.is_dir():
        logger.error("Collection data directory does not exist: %s", collection_data_path)
        return None
    return lrclib.find_tracks_missing_lyrics(collection_data_path)


def _load_transcription_model(
    model_name: str, device: str | None,
) -> media_transcription.TranscriptionModel:
    try:
        faster_whisper = cast(
            media_transcription.FasterWhisper, import_module("faster_whisper"),
        )
    except ImportError:
        logger.exception(
            "Could not import faster_whisper; install with pip install 'mediascripts[lyrics]'"
        )
        raise
    try:
        logger.info(
            "Loading faster-whisper model %s (device=%s, compute_type=int8)",
            model_name, device or "auto",
        )
        return faster_whisper.WhisperModel(
            model_name, device=device or "auto", compute_type="int8",
        )
    except (OSError, RuntimeError, ValueError):
        logger.exception("Could not load faster-whisper model %s", model_name)
        raise


def _record_lookup_cache(
    cache: dict[str, dict[str, str]], track_path: Path, lookup_status: str,
) -> None:
    key = str(track_path.resolve())
    if lookup_status in (STATUS_INSTRUMENTAL, STATUS_NOT_FOUND):
        cache[key] = {"status": lookup_status, "checked": date.today().isoformat()}
        lrclib.save_lookup_cache(cache)


def main_loop(
    sleep_between_requests: float = 0.5,
    lookup_enabled: bool = True,
    transcribe_enabled: bool = True,
    specific_file: Path | None = None,
    specific_dir: Path | None = None,
    retry_not_found: bool = False,
    model_name: str = "small",
    language: str = "en",
    device: str | None = None,
) -> int:
    lrclib.reset_request_timing(sleep_between_requests)
    try:
        tracks = _find_tracks(specific_file, specific_dir)
    except OSError:
        logger.exception("Could not scan lyrics tracks")
        return 1
    if tracks is None:
        return 1
    if not tracks:
        logger.info("No tracks are missing lyrics text files; nothing to do")
        return 0
    if not lookup_enabled and not transcribe_enabled:
        log_missing_lyrics_files(tracks)
        return 0

    cache = lrclib.load_lookup_cache() if lookup_enabled else {}
    random.shuffle(tracks)
    attempts: Counter[Path] = Counter()
    queue = deque(tracks)
    statuses: Counter[str] = Counter()
    transcription_model: media_transcription.TranscriptionModel | None = None
    model_load_failed = False
    had_lookup_request = False

    with tqdm(total=len(tracks), desc="Populating lyrics", unit="track") as progress:
        while queue:
            track_path = queue.popleft()
            if has_lyrics_text(track_path):
                final_status = STATUS_SKIPPED
                lookup_status = STATUS_SKIPPED
            else:
                cached_status = cache.get(str(track_path.resolve()), {}).get("status")
                cached_as_skippable = lookup_enabled and lrclib.is_cached_as_skippable(
                    cache, track_path, retry_not_found,
                )
                lookup_was_attempted = False
                if lookup_enabled and not cached_as_skippable:
                    if had_lookup_request:
                        lrclib.wait_before_request()
                    attempts[track_path] += 1
                    had_lookup_request = True
                    logger.info(
                        "[%s] LRCLIB attempt %d/%d",
                        track_path.name, attempts[track_path], lrclib.LRCLIB_MAX_ATTEMPTS,
                    )
                    lookup_status = lrclib.process_track(track_path)
                    lookup_was_attempted = True
                elif cached_as_skippable:
                    lookup_status = cached_status or STATUS_NOT_FOUND
                else:
                    lookup_status = STATUS_NOT_FOUND

                if lookup_status in (STATUS_SAVED, STATUS_SKIPPED):
                    final_status = lookup_status
                elif (
                    transcribe_enabled
                    and track_path.suffix.lower() in MEDIA_EXTENSIONS
                ):
                    if transcription_model is None and not model_load_failed:
                        try:
                            transcription_model = _load_transcription_model(model_name, device)
                        except (ImportError, OSError, RuntimeError, ValueError):
                            model_load_failed = True
                    if transcription_model is None:
                        final_status = STATUS_ERROR
                    else:
                        final_status = media_transcription.process_track(
                            track_path, transcription_model, language,
                        )
                else:
                    if transcribe_enabled and track_path.suffix.lower() not in MEDIA_EXTENSIONS:
                        logger.info(
                            "[%s] No sibling media file available for local transcription",
                            track_path.name,
                        )
                    final_status = lookup_status

                if lookup_was_attempted:
                    _record_lookup_cache(cache, track_path, lookup_status)
                if final_status == STATUS_SAVED:
                    key = str(track_path.resolve())
                    if cache.pop(key, None) is not None:
                        lrclib.save_lookup_cache(cache)

                if lookup_status == STATUS_RETRY and final_status not in (
                    STATUS_SAVED, STATUS_SKIPPED,
                ):
                    if attempts[track_path] < lrclib.LRCLIB_MAX_ATTEMPTS:
                        queue.append(track_path)
                        logger.warning(
                            "[%s] Deferring LRCLIB retry until after %d other track(s)",
                            track_path.name, len(queue) - 1,
                        )
                        continue
                    logger.error(
                        "[%s] Giving up on LRCLIB after %d attempts this run",
                        track_path.name, attempts[track_path],
                    )
                    final_status = STATUS_ERROR

            statuses[final_status] += 1
            progress.update(1)

    logger.info(
        "Batch complete: %d saved, %d skipped, %d instrumental, %d not found, %d errors",
        statuses[STATUS_SAVED],
        statuses[STATUS_SKIPPED],
        statuses[STATUS_INSTRUMENTAL],
        statuses[STATUS_NOT_FOUND],
        statuses[STATUS_ERROR],
    )
    return 1 if statuses[STATUS_ERROR] else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Populate missing lyrics text using LRCLIB, then local faster-whisper transcription."
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help=(
            "Initial seconds between LRCLIB requests (default: 0.5). "
            "Each HTTP 4xx/5xx response adds 0.5 seconds for the rest of the run."
        ),
    )
    parser.add_argument(
        "--lookup",
        type=parse_boolean,
        default=True,
        metavar="true|false",
        help="Enable LRCLIB lookup (default: true).",
    )
    parser.add_argument(
        "--transcribe",
        type=parse_boolean,
        default=True,
        metavar="true|false",
        help="Enable faster-whisper fallback (default: true).",
    )
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--file",
        type=Path,
        help="Process one media track or same-basename track YAML (.yml/.yaml) file.",
    )
    target_group.add_argument(
        "--dir",
        type=Path,
        help="Process media tracks in a specific album directory instead of scanning all albums.",
    )
    parser.add_argument(
        "--retry-not-found",
        action="store_true",
        help="Query LRCLIB again for tracks previously cached as not found.",
    )
    parser.add_argument("--model", default="small", help="faster-whisper model name or checkpoint path.")
    parser.add_argument(
        "--language", default="en",
        help="Fallback lyrics language code when artist.yml has no language codes (default: en).",
    )
    parser.add_argument("--device", help="faster-whisper device, e.g. cpu or cuda (default: auto).")
    parser.add_argument("--cpu", action="store_true", help="Force CPU mode, overriding --device.")
    args = parser.parse_args()

    try:
        return main_loop(
            sleep_between_requests=args.sleep,
            lookup_enabled=args.lookup,
            transcribe_enabled=args.transcribe,
            specific_file=args.file,
            specific_dir=args.dir,
            retry_not_found=args.retry_not_found,
            model_name=args.model,
            language=args.language,
            device="cpu" if args.cpu else args.device,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
