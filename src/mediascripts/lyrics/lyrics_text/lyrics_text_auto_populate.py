"""
Usage: python src/mediascripts/lyrics/lyrics_text/lyrics_text_auto_populate.py [options]
Or from script symlink: lyrics-text-auto-populate [options]

Searches every album directory in the collection for media tracks that lack a
lyrics text file ("<track-name>.txt", same base filename as the track).
Tracks that already have a lyrics text file are left untouched.
For tracks without one, the plain lyrics are looked up via LRCLIB (https://lrclib.net)
and, if found, written to the lyrics text file. LRC files are ignored.

Tracks that LRCLIB reports as instrumental or cannot find are recorded in a lookup
cache ($XDG_CACHE_HOME/moongas/lyrics_text_lookups.json) so they aren't re-queried
on later runs. Use --retry-not-found to query not-found tracks again.

It expects the environment variable MOONGAS_COLLECTION_ROOTDIR to be set,
pointing to the root directory of the Moongas collection,
i.e. the directory containing the `data` subdirectory,
which in turn contains one or more library directories e.g. `Music`

./lyrics-text-auto-populate

./lyrics-text-auto-populate --lookup=false

./lyrics-text-auto-populate --file "$MOONGAS_COLLECTION_ROOTDIR/data/Music/Slayer/Seasons in the Abyss [1990]/08 - Temptation.mp3"

./lyrics-text-auto-populate --dir "$MOONGAS_COLLECTION_ROOTDIR/data/Music/Slayer/Seasons in the Abyss [1990]"


Command line options:
  --sleep            Seconds to sleep between LRCLIB requests (default: 0.5).
  --lookup           Enable LRCLIB lookups (default: true). Use --lookup=false to only list missing lyrics files.
  --file             Specify a single media track file to process. Must be the full path to the file.
  --dir              Process media tracks in a single album directory; mutually exclusive with --file.
  --retry-not-found  Query LRCLIB again for tracks previously cached as not found.
  --help             Show this help message and exit.
"""

import argparse
import json
import logging
import os
import re
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import mutagen
from mediascan.utils.path.album_path import AlbumPathBuilder
from tqdm import tqdm

from mediascripts.common.cli.parse_boolean import parse_boolean

# Configure with LOG_LEVEL=DEBUG for additional diagnostic detail.
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

MOONGAS_COLLECTION_ROOTDIR = os.environ.get(
    "MOONGAS_COLLECTION_ROOTDIR", "../../moongas-collection-demo"
)

MEDIA_EXTENSIONS = {
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
}

LYRICS_TEXT_EXTENSION = ".txt"

# Leading track numbers e.g. "01 - ", "01.01 - ", "01-01- ", "1. ", "03 "
TRACK_NUMBER_PATTERN = re.compile(r"^\d+(?:[.\-]\d+)?\s*[-.]?\s*")

LRC_TIMESTAMP_PATTERN = re.compile(r"^\s*(?:\[\d+:\d+(?:[.:]\d+)?\])+\s?", re.MULTILINE)

LRCLIB_BASE_URL = "https://lrclib.net"
LRCLIB_MAX_ATTEMPTS = 5
# LRCLIB only matches /api/get when durations are within ±2 seconds
DURATION_TOLERANCE_SECONDS = 2

try:
    _package_version = version("mediascripts")
except PackageNotFoundError:
    _package_version = "dev"
# LRCLIB requires clients to identify themselves
USER_AGENT = (
    f"moongas-mediascripts v{_package_version} "
    "(https://github.com/moongas-org/moongas-mediasripts-python)"
)

LOOKUP_CACHE_PATH = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "moongas"
    / "lyrics_text_lookups.json"
)
STATUS_SAVED = "saved"
STATUS_INSTRUMENTAL = "instrumental"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"


@dataclass
class TrackInfo:
    artist: str
    title: str
    album: str
    duration: Optional[int]


def lyrics_text_path_for(track_path: Path) -> Path:
    return track_path.with_suffix(LYRICS_TEXT_EXTENSION)


def track_title_from_filename(track_path: Path) -> str:
    title = TRACK_NUMBER_PATTERN.sub("", track_path.stem).strip()
    return title or track_path.stem


def _first_tag(tags: Optional[mutagen.Tags], key: str) -> str:
    values = tags.get(key) if tags else None
    return str(values[0]).strip() if values else ""


def read_track_info(track_path: Path) -> TrackInfo:
    """Prefer embedded tags, falling back to the artist/album directory and file names."""
    album_path = AlbumPathBuilder.of(track_path.parent)
    info = TrackInfo(
        artist=album_path.artist_dirname,
        title=track_title_from_filename(track_path),
        album=re.sub(r"\s*\[[^\]]*\]\s*$", "", album_path.album_dirname),
        duration=None,
    )
    try:
        media = mutagen.File(track_path, easy=True)
    except mutagen.MutagenError as e:
        logger.debug("[%s] Could not read tags: %s", track_path.name, e)
        return info
    if media is None:
        return info
    info.artist = _first_tag(media.tags, "artist") or info.artist
    info.title = _first_tag(media.tags, "title") or info.title
    info.album = _first_tag(media.tags, "album") or info.album
    length = media.info.length
    if length:
        info.duration = round(length)
    return info


def lrclib_request(endpoint: str, params: dict[str, Any]) -> Any:
    """Return the decoded JSON response, or None on 404."""
    url = f"{LRCLIB_BASE_URL}{endpoint}?{urlencode(params)}"
    for attempt in range(1, LRCLIB_MAX_ATTEMPTS + 1):
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 503) and attempt < LRCLIB_MAX_ATTEMPTS:
                try:
                    retry_after = float(e.headers.get("Retry-After", "5"))
                except ValueError:
                    retry_after = 5.0
                logger.warning(
                    "LRCLIB returned HTTP %d; retrying in %.1f seconds",
                    e.code,
                    retry_after,
                )
                time.sleep(retry_after)
                continue
            raise
    return None


def durations_match(record: dict[str, Any], info: TrackInfo) -> bool:
    if info.duration is None or not record.get("duration"):
        return True
    return abs(record["duration"] - info.duration) <= DURATION_TOLERANCE_SECONDS


def lookup_lyrics(info: TrackInfo) -> Optional[dict[str, Any]]:
    params: dict[str, Any] = {
        "artist_name": info.artist,
        "track_name": info.title,
        "album_name": info.album,
    }
    if info.duration is not None and 1 <= info.duration <= 3600:
        params["duration"] = info.duration
    record = lrclib_request("/api/get", params)
    if record:
        return record

    # Fall back to search, which tolerates album name differences
    results: list[dict[str, Any]] = (
        lrclib_request(
            "/api/search", {"artist_name": info.artist, "track_name": info.title}
        )
        or []
    )
    for result in results:
        if result.get("trackName", "").casefold() != info.title.casefold():
            continue
        if durations_match(result, info):
            return result
    return None


def plain_lyrics_from_record(record: dict[str, Any]) -> str:
    plain = (record.get("plainLyrics") or "").strip()
    if plain:
        return plain
    synced = record.get("syncedLyrics") or ""
    return LRC_TIMESTAMP_PATTERN.sub("", synced).strip()


def load_lookup_cache() -> dict[str, dict[str, str]]:
    if not LOOKUP_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(LOOKUP_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Ignoring unreadable lookup cache %s: %s", LOOKUP_CACHE_PATH, e)
        return {}


def save_lookup_cache(cache: dict[str, dict[str, str]]) -> None:
    LOOKUP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=LOOKUP_CACHE_PATH.parent,
        suffix=".tmp",
        delete=False,
    ) as temporary_file:
        json.dump(cache, temporary_file, indent=1, sort_keys=True)
    os.replace(temporary_file.name, LOOKUP_CACHE_PATH)


def is_cached_as_skippable(
    cache: dict[str, dict[str, str]], track_path: Path, retry_not_found: bool
) -> bool:
    status = cache.get(str(track_path.resolve()), {}).get("status")
    if status == STATUS_INSTRUMENTAL:
        return True
    return status == STATUS_NOT_FOUND and not retry_not_found


def find_tracks_missing_lyrics(collection_data_path: Path) -> list[Path]:
    """Find media tracks in album directories without a sibling lyrics text file."""
    missing: list[Path] = []
    for root, _, filenames in os.walk(collection_data_path, followlinks=True):
        root_path = Path(root)
        if not AlbumPathBuilder.of(root_path).valid:
            continue
        for filename in filenames:
            track_path = root_path / filename
            if track_path.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            if lyrics_text_path_for(track_path).exists():
                continue
            missing.append(track_path)
    missing.sort()
    logger.info("Found %d track(s) missing lyrics text files", len(missing))
    for track_path in missing:
        logger.debug("Missing lyrics text file for: %s", track_path)
    return missing


def find_tracks_missing_lyrics_in_directory(album_dir: Path) -> list[Path]:
    """Find media tracks directly in an album directory without lyrics text files."""
    missing = sorted(
        track_path
        for track_path in album_dir.iterdir()
        if track_path.is_file()
        and track_path.suffix.lower() in MEDIA_EXTENSIONS
        and not lyrics_text_path_for(track_path).exists()
    )
    logger.info("Found %d track(s) missing lyrics text files", len(missing))
    for track_path in missing:
        logger.debug("Missing lyrics text file for: %s", track_path)
    return missing


def process_track(track_path: Path) -> str:
    filename = track_path.name
    lyrics_path = lyrics_text_path_for(track_path)
    temporary_path: Path | None = None

    info = read_track_info(track_path)
    logger.info(
        "[%s] Looking up artist=%r title=%r album=%r duration=%s",
        filename,
        info.artist,
        info.title,
        info.album,
        info.duration,
    )

    try:
        record = lookup_lyrics(info)
        if record is None:
            logger.warning("[%s] LRCLIB has no lyrics for this track", filename)
            return STATUS_NOT_FOUND
        if record.get("instrumental"):
            logger.info("[%s] LRCLIB reports this track is instrumental", filename)
            return STATUS_INSTRUMENTAL
        content = plain_lyrics_from_record(record)
        if not content:
            logger.warning(
                "[%s] LRCLIB record %s has no lyrics", filename, record.get("id")
            )
            return STATUS_NOT_FOUND

        logger.debug("[%s] Lyrics found:\n%s", filename, content)

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=track_path.parent,
            prefix=f".{lyrics_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(content + "\n")
            temporary_path = Path(temporary_file.name)

        if lyrics_path.exists():
            logger.info(
                "[%s] Lyrics text file appeared meanwhile; not overwriting", filename
            )
            return STATUS_SAVED
        os.replace(temporary_path, lyrics_path)
        temporary_path = None

        logger.info(
            "[%s] Saved %d characters from LRCLIB record %s to %s",
            filename,
            len(content),
            record.get("id"),
            lyrics_path,
        )
        return STATUS_SAVED

    except Exception as e:
        logger.exception("[%s] Failed (%s): %s", filename, type(e).__name__, e)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return STATUS_ERROR


def main_loop(
    sleep_between_requests: float,
    lookup_enabled: bool = True,
    specific_file: Optional[Path] = None,
    retry_not_found: bool = False,
    specific_dir: Optional[Path] = None,
):
    collection_data_path = Path(MOONGAS_COLLECTION_ROOTDIR) / "data"
    logger.info("Collection data directory: %s", collection_data_path.resolve())

    if specific_file is not None:
        if not specific_file.is_file():
            logger.error("Specified file does not exist: %s", specific_file)
            return
        if specific_file.suffix.lower() not in MEDIA_EXTENSIONS:
            logger.error("Specified file is not a media track: %s", specific_file)
            return
        tracks = [] if lyrics_text_path_for(specific_file).exists() else [specific_file]
        # Explicitly requested files are always looked up
        retry_not_found = True
    elif specific_dir is not None:
        if not specific_dir.is_dir():
            logger.error("Specified directory does not exist: %s", specific_dir)
            return
        tracks = find_tracks_missing_lyrics_in_directory(specific_dir)
        # Explicitly requested directories are always looked up
        retry_not_found = True
    else:
        if not collection_data_path.is_dir():
            logger.error(
                "Collection data directory does not exist: %s", collection_data_path
            )
            return
        tracks = find_tracks_missing_lyrics(collection_data_path)

    if not tracks:
        logger.info("No tracks are missing lyrics text files; nothing to do")
        return
    if not lookup_enabled:
        logger.info("Lookup disabled; exiting after listing missing lyrics files")
        for track_path in tracks:
            logger.info("Missing: %s", lyrics_text_path_for(track_path))
        return

    cache = load_lookup_cache()
    pending = [
        t for t in tracks if not is_cached_as_skippable(cache, t, retry_not_found)
    ]
    logger.info(
        "Skipping %d track(s) cached as instrumental/not found (cache: %s)",
        len(tracks) - len(pending),
        LOOKUP_CACHE_PATH,
    )

    statuses: Counter[str] = Counter()
    for index, track_path in enumerate(
        tqdm(pending, desc="Looking up lyrics", unit="track")
    ):
        if index:
            time.sleep(sleep_between_requests)
        status = process_track(track_path)
        statuses[status] += 1
        key = str(track_path.resolve())
        if status in (STATUS_INSTRUMENTAL, STATUS_NOT_FOUND):
            cache[key] = {"status": status, "checked": date.today().isoformat()}
            save_lookup_cache(cache)
        elif status == STATUS_SAVED and cache.pop(key, None) is not None:
            save_lookup_cache(cache)

    logger.info(
        "Batch complete: %d saved, %d instrumental, %d not found, %d errors",
        statuses[STATUS_SAVED],
        statuses[STATUS_INSTRUMENTAL],
        statuses[STATUS_NOT_FOUND],
        statuses[STATUS_ERROR],
    )


def main():
    parser = argparse.ArgumentParser(
        description="Populate missing lyrics text files for media tracks using LRCLIB."
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help="Seconds to sleep between LRCLIB requests (default: 0.5).",
    )
    parser.add_argument(
        "--lookup",
        type=parse_boolean,
        default=True,
        metavar="true|false",
        help="Enable LRCLIB lookups (default: true). Use --lookup=false to only list missing lyrics files.",
    )
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Process a single specific media track file instead of scanning all album directories.",
    )
    target_group.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Process media tracks in a specific album directory instead of scanning all album directories.",
    )
    parser.add_argument(
        "--retry-not-found",
        action="store_true",
        help="Query LRCLIB again for tracks previously cached as not found.",
    )
    args = parser.parse_args()

    try:
        main_loop(
            sleep_between_requests=args.sleep,
            lookup_enabled=args.lookup,
            specific_file=args.file,
            retry_not_found=args.retry_not_found,
            specific_dir=args.dir,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting")


if __name__ == "__main__":
    main()
