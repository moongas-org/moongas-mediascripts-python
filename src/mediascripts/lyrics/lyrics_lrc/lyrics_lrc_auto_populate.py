"""Align existing UTF-8 lyrics text to local media and save missing sibling LRC files.

Run lyrics-lrc-auto-populate with --file, --dir, or no target for collection album
directories under $MOONGAS_COLLECTION_ROOTDIR/data. --dir is non-recursive.
--file accepts media or a track YAML (.yml/.yaml) with same-basename sibling media.
Use --align=false (or --lookup=false) to list eligible tracks without loading
Whisper. Install the optional [lyrics] dependencies and FFmpeg for alignment.
Model weights may be downloaded on first use; alignment itself runs locally.
"""

import argparse
import logging
import math
from collections import Counter
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

from tqdm import tqdm

from mediascripts.common.cli.parse_boolean import parse_boolean
from mediascripts.lyrics.lyrics_common import (
    MEDIA_EXTENSIONS,
    MOONGAS_COLLECTION_ROOTDIR,
    STATUS_ERROR,
    STATUS_SAVED,
    STATUS_SKIPPED,
    configure_logging,
    lyrics_lrc_path_for,
    lyrics_text_path_for,
    resolve_track_file,
    save_lyrics_file,
)
from mediascripts.track.track_yaml.track_yaml_common import iter_collection_files

configure_logging()
logger = logging.getLogger(__name__)


class AlignedSegment(Protocol):
    @property
    def start(self) -> float: ...

    @property
    def text(self) -> str: ...


class AlignmentResult(Protocol):
    @property
    def segments(self) -> Sequence[AlignedSegment]: ...


class AlignmentModel(Protocol):
    def align(
        self, audio: str, text: str, *, language: str, original_split: bool,
    ) -> AlignmentResult | None: ...


class StableWhisper(Protocol):
    def load_model(self, name: str, *, device: str | None) -> AlignmentModel: ...


def is_eligible(track_path: Path) -> bool:
    lrc_path = lyrics_lrc_path_for(track_path)
    return (
        track_path.is_file()
        and track_path.suffix.lower() in MEDIA_EXTENSIONS
        and lyrics_text_path_for(track_path).is_file()
        and not lrc_path.exists()
        and not lrc_path.is_symlink()
    )


def find_tracks_missing_lrc(path: Path, collection: bool = False) -> list[Path]:
    candidates = iter_collection_files(path) if collection else path.iterdir()
    tracks = sorted(track for track in candidates if is_eligible(track))
    logger.info("Found %d track(s) with text lyrics and no LRC file", len(tracks))
    return tracks


def format_lrc(result: AlignmentResult) -> str:
    lines: list[str] = []
    previous_start = -1.0
    for segment in result.segments:
        text = segment.text.strip()
        if not text:
            continue
        start = segment.start
        if not math.isfinite(start) or start < 0 or start < previous_start:
            raise ValueError(f"Invalid or out-of-order alignment timestamp: {start}")
        if "\n" in text or "\r" in text:
            raise ValueError("Aligned segment contains multiple lyrics lines")
        previous_start = start
        centiseconds = round(start * 100)
        minutes, remainder = divmod(centiseconds, 6000)
        seconds, fraction = divmod(remainder, 100)
        lines.append(f"[{minutes:02d}:{seconds:02d}.{fraction:02d}]{text}")
    if not lines:
        raise ValueError("Alignment returned no timed lyrics")
    return "\n".join(lines) + "\n"


def process_track(track_path: Path, model: AlignmentModel, language: str = "en") -> str:
    if not is_eligible(track_path):
        logger.info("[%s] No longer eligible; skipping", track_path)
        return STATUS_SKIPPED
    lrc_path = lyrics_lrc_path_for(track_path)
    try:
        text = lyrics_text_path_for(track_path).read_text(encoding="utf-8-sig").strip()
        if not text:
            raise ValueError("Lyrics text file is empty")
        logger.info("[%s] Aligning existing lyrics locally (language=%s)", track_path, language)
        result = model.align(str(track_path), text, language=language, original_split=True)
        if result is None:
            raise ValueError("Alignment failed to produce a result")
        content = format_lrc(result)
        try:
            save_lyrics_file(lrc_path, content)
        except FileExistsError:
            logger.info("[%s] LRC file appeared meanwhile; not overwriting", track_path)
            return STATUS_SKIPPED
        logger.info("[%s] Saved aligned lyrics to %s", track_path, lrc_path)
        return STATUS_SAVED
    except (OSError, UnicodeError, ValueError, RuntimeError, TypeError):
        logger.exception("[%s] Failed to generate LRC lyrics", track_path)
        return STATUS_ERROR


def main_loop(
    align_enabled: bool = True,
    specific_file: Path | None = None,
    specific_dir: Path | None = None,
    model_name: str = "small",
    language: str = "en",
    device: str | None = None,
) -> int:
    if specific_file is not None:
        try:
            specific_file = resolve_track_file(specific_file)
        except (OSError, ValueError) as error:
            logger.error("Could not select track: %s", error)
            return 1
        if specific_file.suffix.lower() not in MEDIA_EXTENSIONS:
            logger.error("No same-basename media file beside track YAML: %s", specific_file)
            return 1
        tracks = [specific_file] if is_eligible(specific_file) else []
    else:
        path = specific_dir if specific_dir is not None else Path(MOONGAS_COLLECTION_ROOTDIR) / "data"
        if not path.is_dir():
            logger.error("Directory does not exist: %s", path)
            return 1
        try:
            tracks = find_tracks_missing_lrc(path, collection=specific_dir is None)
        except OSError:
            logger.exception("Could not scan directory: %s", path)
            return 1
    if not tracks:
        logger.info("No tracks have text lyrics and a missing LRC file; nothing to do")
        return 0
    if not align_enabled:
        for track in tracks:
            logger.info("Missing: %s", lyrics_lrc_path_for(track))
        return 0

    try:
        stable_whisper = cast(StableWhisper, import_module("stable_whisper"))
    except ImportError:
        logger.exception("Could not import stable_whisper; install with pip install 'mediascripts[lyrics]'")
        return 1
    try:
        logger.info("Loading Whisper model %s (device=%s)", model_name, device or "auto")
        model = stable_whisper.load_model(model_name, device=device)
    except (OSError, RuntimeError, ValueError):
        logger.exception("Could not load Whisper model %s", model_name)
        return 1

    statuses: Counter[str] = Counter()
    for track in tqdm(tracks, desc="Aligning lyrics", unit="track"):
        statuses[process_track(track, model, language)] += 1
    logger.info(
        "Batch complete: %d saved, %d skipped, %d errors",
        statuses[STATUS_SAVED], statuses[STATUS_SKIPPED], statuses[STATUS_ERROR],
    )
    return 1 if statuses[STATUS_ERROR] else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Populate missing LRC files by aligning existing text lyrics to local media using stable-ts."
    )
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument(
        "--file", type=Path, help="Process one media track or same-basename track YAML (.yml/.yaml) file.",
    )
    targets.add_argument("--dir", type=Path, help="Process media tracks directly in this album directory.")
    parser.add_argument(
        "--align", "--lookup", dest="align_enabled", type=parse_boolean, default=True,
        metavar="true|false", help="Enable local alignment (default: true); false only lists eligible tracks.",
    )
    parser.add_argument("--model", default="small", help="Whisper model name or checkpoint path (default: small).")
    parser.add_argument("--language", default="en", help="Lyrics language code (default: en).")
    parser.add_argument("--device", help="PyTorch device, e.g. cpu or cuda (default: auto).")
    parser.add_argument("--cpu", action="store_true", help="Force CPU mode, overriding --device.")
    args = parser.parse_args()
    try:
        return main_loop(
            align_enabled=args.align_enabled, specific_file=args.file, specific_dir=args.dir,
            model_name=args.model, language=args.language, device="cpu" if args.cpu else args.device,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
