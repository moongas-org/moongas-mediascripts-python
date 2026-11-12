"""Transcribe local media and save timestamped sibling LRC files.

Run lyrics-lrc-auto-populate with --file, --dir, or no target for collection album
directories under $MOONGAS_COLLECTION_ROOTDIR/data. --dir is non-recursive.
--file accepts media or a track YAML (.yml/.yaml) with same-basename sibling media.
Use --use-lyrics-txt to force-align existing text lyrics when available.
Use --align=false (or --lookup=false) to list eligible tracks without loading a model.
Install the optional [lyrics] dependencies for transcription.
Install FFmpeg and [lyrics-align] as well to use --use-lyrics-txt.
"""

import argparse
import logging
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from subprocess import SubprocessError
from typing import Protocol, cast

from tqdm import tqdm

from mediascripts.common.cli.parse_boolean import parse_boolean
from mediascripts.common.files import shuffled_files
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


class AlignedWord(Protocol):
    @property
    def start(self) -> float | None: ...


class LyricsAligner(Protocol):
    def align(self, audio_path: str, text: str) -> list[AlignedWord]: ...


class AudioBuffer(Protocol):
    def __len__(self) -> int: ...


class WhisperX(Protocol):
    def load_align_model(self, *, language_code: str, device: str) -> tuple[object, object]: ...

    def load_audio(self, path: str) -> AudioBuffer: ...

    def align(
        self,
        transcript: Sequence[dict[str, object]],
        model: object,
        metadata: object,
        audio: AudioBuffer,
        device: str,
        *,
        return_char_alignments: bool,
    ) -> Mapping[str, object]: ...


class WhisperXLyricsAligner:
    def __init__(self, whisperx: WhisperX, language: str, device: str) -> None:
        self.whisperx = whisperx
        self.device = device
        self.model, self.metadata = whisperx.load_align_model(
            language_code=language, device=device,
        )

    def align(self, audio_path: str, text: str) -> list[AlignedWord]:
        audio = self.whisperx.load_audio(audio_path)
        transcript: list[dict[str, object]] = [
            {"start": 0.0, "end": len(audio) / 16000, "text": " ".join(text.split())},
        ]
        result = self.whisperx.align(
            transcript, self.model, self.metadata, audio, self.device,
            return_char_alignments=False,
        )
        raw_words = result.get("word_segments")
        if not isinstance(raw_words, list):
            raise ValueError("Forced alignment returned no word timestamps")
        words: list[AlignedWord] = []
        for raw_word in cast(list[object], raw_words):
            if not isinstance(raw_word, Mapping):
                raise ValueError("Forced alignment returned an invalid word")
            word_data = cast(Mapping[str, object], raw_word)
            start = word_data.get("start")
            if isinstance(start, (int, float)):
                words.append(TimedWord(float(start)))
            else:
                words.append(TimedWord(None))
        return words


@dataclass
class TimedWord:
    start: float | None


def is_eligible(track_path: Path, use_lyrics_txt: bool = False) -> bool:
    lrc_path = lyrics_lrc_path_for(track_path)
    return (
        track_path.is_file()
        and track_path.suffix.lower() in MEDIA_EXTENSIONS
        and (not use_lyrics_txt or lyrics_text_path_for(track_path).is_file())
        and not lrc_path.exists()
        and not lrc_path.is_symlink()
    )


def find_tracks_missing_lrc(
    path: Path, collection: bool = False, use_lyrics_txt: bool = False,
) -> list[Path]:
    candidates = iter_collection_files(path) if collection else path.iterdir()
    tracks = sorted(
        track for track in candidates if is_eligible(track, use_lyrics_txt=use_lyrics_txt)
    )
    logger.info("Found %d track(s) eligible for LRC generation", len(tracks))
    return tracks


def format_lrc(segments: Iterable[TranscribedSegment]) -> str:
    return _format_timed_lines((segment.start, segment.text) for segment in segments)


def format_lyrics_text_lrc(
    text: str, words: Sequence[AlignedWord], language: str = "en",
) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if language in {"ja", "zh"}:
        word_counts = [sum(not character.isspace() for character in line) for line in lines]
    else:
        word_counts = [len(line.split()) for line in lines]
    if not lines or sum(word_counts) != len(words):
        raise ValueError("Forced alignment word count does not match lyrics text")
    timed_lines: list[tuple[float, str]] = []
    word_index = 0
    for line, word_count in zip(lines, word_counts):
        word = words[word_index]
        if word.start is None:
            raise ValueError(f"Could not align the first word of lyrics line: {line}")
        timed_lines.append((word.start, line))
        word_index += word_count
    return _format_timed_lines(timed_lines)


def _format_timed_lines(lines: Iterable[tuple[float, str]]) -> str:
    formatted_lines: list[str] = []
    previous_start = -1.0
    for start, text in lines:
        text = text.strip()
        if not text:
            continue
        if not math.isfinite(start) or start < 0 or start < previous_start:
            raise ValueError(f"Invalid or out-of-order alignment timestamp: {start}")
        if "\n" in text or "\r" in text:
            raise ValueError("Lyrics segment contains multiple lines")
        previous_start = start
        centiseconds = round(start * 100)
        minutes, remainder = divmod(centiseconds, 6000)
        seconds, fraction = divmod(remainder, 100)
        formatted_lines.append(f"[{minutes:02d}:{seconds:02d}.{fraction:02d}] {text}")
    if not formatted_lines:
        raise ValueError("Transcription returned no timed lyrics")
    return "\n".join(formatted_lines) + "\n"


def process_track(
    track_path: Path,
    model: TranscriptionModel | LyricsAligner,
    language: str = "en",
    use_lyrics_txt: bool = False,
) -> str:
    if not is_eligible(track_path, use_lyrics_txt=use_lyrics_txt):
        logger.info("[%s] No longer eligible; skipping", track_path)
        return STATUS_SKIPPED
    lrc_path = lyrics_lrc_path_for(track_path)
    try:
        if use_lyrics_txt:
            text = lyrics_text_path_for(track_path).read_text(encoding="utf-8-sig").strip()
            if not text:
                raise ValueError("Lyrics text file is empty")
            logger.info("[%s] Aligning supplied lyrics locally (language=%s)", track_path, language)
            aligner = cast(LyricsAligner, model)
            aligned_words = aligner.align(str(track_path), text)
            content = format_lyrics_text_lrc(text, aligned_words, language)
        else:
            logger.info("[%s] Transcribing audio locally (language=%s)", track_path, language)
            transcriber = cast(TranscriptionModel, model)
            segments, _ = transcriber.transcribe(str(track_path), language=language)
            content = format_lrc(segments)
        try:
            save_lyrics_file(lrc_path, content)
        except FileExistsError:
            logger.info("[%s] LRC file appeared meanwhile; not overwriting", track_path)
            return STATUS_SKIPPED
        logger.info("[%s] Saved lyrics to %s", track_path, lrc_path)
        return STATUS_SAVED
    except (OSError, UnicodeError, ValueError, RuntimeError, TypeError, SubprocessError):
        logger.exception("[%s] Failed to generate LRC lyrics", track_path)
        return STATUS_ERROR


def main_loop(
    align_enabled: bool = True,
    specific_file: Path | None = None,
    specific_dir: Path | None = None,
    model_name: str = "small",
    language: str = "en",
    device: str | None = None,
    use_lyrics_txt: bool = False,
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
        if use_lyrics_txt and not lyrics_text_path_for(specific_file).is_file():
            logger.error("--use-lyrics-txt requires a sibling text lyrics file: %s", specific_file)
            return 1
        tracks = [specific_file] if is_eligible(specific_file, use_lyrics_txt=use_lyrics_txt) else []
    else:
        path = specific_dir if specific_dir is not None else Path(MOONGAS_COLLECTION_ROOTDIR) / "data"
        if not path.is_dir():
            logger.error("Directory does not exist: %s", path)
            return 1
        try:
            tracks = find_tracks_missing_lrc(
                path, collection=specific_dir is None, use_lyrics_txt=use_lyrics_txt,
            )
        except OSError:
            logger.exception("Could not scan directory: %s", path)
            return 1
    if not tracks:
        logger.info("No tracks are eligible for LRC generation; nothing to do")
        return 0
    if not align_enabled:
        for track in tracks:
            logger.info("Missing: %s", lyrics_lrc_path_for(track))
        return 0

    if use_lyrics_txt:
        try:
            whisperx = cast(WhisperX, import_module("whisperx"))
            if device is None or device == "auto":
                torch = import_module("torch")
                device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info("Loading forced-alignment model (language=%s, device=%s)", language, device)
            model: TranscriptionModel | LyricsAligner = WhisperXLyricsAligner(
                whisperx, language, device,
            )
        except ImportError:
            logger.exception(
                "Could not import whisperx; install with pip install 'mediascripts[lyrics-align]'"
            )
            return 1
        except (OSError, RuntimeError, ValueError):
            logger.exception("Could not load forced-alignment model for %s", language)
            return 1
    else:
        try:
            faster_whisper = cast(FasterWhisper, import_module("faster_whisper"))
        except ImportError:
            logger.exception(
                "Could not import faster_whisper; install with pip install 'mediascripts[lyrics]'"
            )
            return 1
        try:
            logger.info(
                "Loading faster-whisper model %s (device=%s, compute_type=int8)",
                model_name, device or "auto",
            )
            model = faster_whisper.WhisperModel(
                model_name, device=device or "auto", compute_type="int8",
            )
        except (OSError, RuntimeError, ValueError):
            logger.exception("Could not load faster-whisper model %s", model_name)
            return 1

    tracks = shuffled_files(tracks)
    statuses: Counter[str] = Counter()
    progress_description = "Aligning lyrics" if use_lyrics_txt else "Transcribing lyrics"
    for track in tqdm(tracks, desc=progress_description, unit="track"):
        statuses[process_track(track, model, language, use_lyrics_txt)] += 1
    logger.info(
        "Batch complete: %d saved, %d skipped, %d errors",
        statuses[STATUS_SAVED], statuses[STATUS_SKIPPED], statuses[STATUS_ERROR],
    )
    return 1 if statuses[STATUS_ERROR] else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Populate missing LRC files by transcribing local media using faster-whisper."
    )
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument(
        "--file", type=Path, help="Process one media track or same-basename track YAML (.yml/.yaml) file.",
    )
    targets.add_argument("--dir", type=Path, help="Process media tracks directly in this album directory.")
    parser.add_argument(
        "--align", "--lookup", dest="align_enabled", type=parse_boolean, default=True,
        metavar="true|false", help="Enable local LRC generation (default: true); false only lists eligible tracks.",
    )
    parser.add_argument(
        "--use-lyrics-txt", action="store_true",
        help="Force-align existing sibling .txt lyrics instead of writing recognized lyrics.",
    )
    parser.add_argument(
        "--model", default="small",
        help="faster-whisper model name or checkpoint path (default: small; ignored with --use-lyrics-txt).",
    )
    parser.add_argument("--language", default="en", help="Lyrics language code (default: en).")
    parser.add_argument("--device", help="Model device, e.g. cpu or cuda (default: auto).")
    parser.add_argument("--cpu", action="store_true", help="Force CPU mode, overriding --device.")
    args = parser.parse_args()
    try:
        return main_loop(
            align_enabled=args.align_enabled, specific_file=args.file, specific_dir=args.dir,
            model_name=args.model, language=args.language, device="cpu" if args.cpu else args.device,
            use_lyrics_txt=args.use_lyrics_txt,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
