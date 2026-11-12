"""Transcribe local media into missing sibling UTF-8 lyrics text files.

Run lyrics-text-auto-populate-from-file with --file, --dir, or no target for
collection album directories under $MOONGAS_COLLECTION_ROOTDIR/data.
--dir is non-recursive; --file also accepts track YAML with sibling media.
Use --transcribe=false (or --lookup=false) to list missing files without loading
faster-whisper. Install the optional [lyrics] dependencies for transcription.
Model weights may download on first use; transcription itself runs locally.
Existing text lyrics are never overwritten. LRC files and embedded tags are
not used; speech recognition may be inaccurate for singing or instrumental audio.
"""

import argparse
import logging
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from subprocess import SubprocessError
from typing import cast

from dataclass_wizard.v0.errors import JSONWizardError
from mediascan.types.artist.artist_yaml_file import ArtistYamlFile
from mediascan.types.artist.artist_yaml_file_loader import load_artist_yaml_file
from tqdm import tqdm
from yaml import YAMLError

from mediascripts.common.cli.parse_boolean import parse_boolean
from mediascripts.common.files import shuffled_files
from mediascripts.lyrics.lyrics_common import (
    MEDIA_EXTENSIONS,
    MOONGAS_COLLECTION_ROOTDIR,
    STATUS_ERROR,
    STATUS_SAVED,
    STATUS_SKIPPED,
    configure_logging,
    lyrics_text_path_for,
    resolve_track_file,
)
from mediascripts.lyrics.lyrics_text.lyrics_text_common import (
    TranscribedSegment,
    TranscriptionModel,
    find_tracks_missing_lyrics as find_missing_lyrics,
    is_eligible,
    load_transcription_model,
    log_missing_lyrics_files,
    save_lyrics_file_if_missing,
)
from mediascripts.track.track_yaml.track_yaml_common import iter_collection_files

configure_logging()
logger = logging.getLogger(__name__)


def find_tracks_missing_lyrics(path: Path, collection: bool = False) -> list[Path]:
    candidates = iter_collection_files(path) if collection else path.iterdir()
    return find_missing_lyrics(candidates)


def format_lyrics(segments: Iterable[TranscribedSegment]) -> str:
    lines = [segment.text.strip() for segment in segments if segment.text.strip()]
    if not lines:
        raise ValueError("Transcription returned no lyrics text")
    return "\n".join(lines) + "\n"


def language_for_track(track_path: Path, fallback: str) -> str:
    artist_yaml = track_path.parent.parent / "artist.yml"
    try:
        artist = cast(object, load_artist_yaml_file(artist_yaml))
    except FileNotFoundError:
        return fallback
    if not isinstance(artist, ArtistYamlFile):
        raise ValueError(f"Expected an artist mapping in {artist_yaml}")
    language_codes = artist.artist_data.language_codes
    if not language_codes:
        return fallback
    if not language_codes[0]:
        raise ValueError(f"First language code is empty in {artist_yaml}")
    return language_codes[0]


def process_track(track_path: Path, model: TranscriptionModel, language: str = "en") -> str:
    if not is_eligible(track_path):
        logger.info("[%s] No longer eligible; skipping", track_path)
        return STATUS_SKIPPED
    lyrics_path = lyrics_text_path_for(track_path)
    try:
        language = language_for_track(track_path, language)
        logger.info("[%s] Transcribing audio locally (language=%s)", track_path, language)
        segments, _ = model.transcribe(str(track_path), language=language)
        content = format_lyrics(segments)
        if not save_lyrics_file_if_missing(lyrics_path, content):
            logger.info("[%s] Lyrics text file appeared meanwhile; not overwriting", track_path)
            return STATUS_SKIPPED
        logger.info("[%s] Saved transcribed lyrics to %s", track_path, lyrics_path)
        return STATUS_SAVED
    except (OSError, UnicodeError, ValueError, RuntimeError, TypeError, SubprocessError, YAMLError, JSONWizardError):
        logger.exception("[%s] Failed to transcribe lyrics", track_path)
        return STATUS_ERROR


def main_loop(
    transcribe_enabled: bool = True,
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
            tracks = find_tracks_missing_lyrics(path, collection=specific_dir is None)
        except OSError:
            logger.exception("Could not scan directory: %s", path)
            return 1
    if not tracks:
        logger.info("No tracks are missing lyrics text files; nothing to do")
        return 0
    if not transcribe_enabled:
        log_missing_lyrics_files(tracks)
        return 0

    try:
        model = load_transcription_model(model_name, device)
    except (ImportError, OSError, RuntimeError, ValueError):
        return 1

    statuses: Counter[str] = Counter()
    for track in tqdm(shuffled_files(tracks), desc="Transcribing lyrics", unit="track"):
        statuses[process_track(track, model, language)] += 1
    logger.info(
        "Batch complete: %d saved, %d skipped, %d errors",
        statuses[STATUS_SAVED], statuses[STATUS_SKIPPED], statuses[STATUS_ERROR],
    )
    return 1 if statuses[STATUS_ERROR] else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Populate missing lyrics text files by transcribing local media using faster-whisper."
    )
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument(
        "--file", type=Path, help="Process one media track or same-basename track YAML (.yml/.yaml) file.",
    )
    targets.add_argument("--dir", type=Path, help="Process media tracks directly in this album directory.")
    parser.add_argument(
        "--transcribe", "--lookup", dest="transcribe_enabled", type=parse_boolean, default=True,
        metavar="true|false", help="Enable local transcription (default: true); false only lists eligible tracks.",
    )
    parser.add_argument("--model", default="small", help="faster-whisper model name or checkpoint path (default: small).")
    parser.add_argument(
        "--language", default="en",
        help="Fallback lyrics language code when artist.yml has no language codes (default: en).",
    )
    parser.add_argument("--device", help="faster-whisper device, e.g. cpu or cuda (default: auto).")
    parser.add_argument("--cpu", action="store_true", help="Force CPU mode, overriding --device.")
    args = parser.parse_args()
    try:
        return main_loop(
            transcribe_enabled=args.transcribe_enabled, specific_file=args.file, specific_dir=args.dir,
            model_name=args.model, language=args.language, device="cpu" if args.cpu else args.device,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
