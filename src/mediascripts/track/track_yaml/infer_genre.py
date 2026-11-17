"""Validate genres and infer an artist's genre from neighboring tracks."""

import logging
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path

import mutagen
from mediascan.types.genre.genres import Genre
from mutagen.id3 import ID3
from ruamel.yaml.error import YAMLError

from mediascripts.lyrics.lyrics_common import MEDIA_EXTENSIONS

logger = logging.getLogger(__name__)


def filter_valid_genres(genres: Iterable[str]) -> list[str]:
    valid_genres = {genre.value for genre in Genre}
    return [genre for genre in genres if genre in valid_genres]


def infer_artist_genre(
    path: Path, *,
    read_track_values: Callable[[Path], dict[str, str]],
    load_media: Callable[[Path], ID3 | mutagen.FileType],
    text_values: Callable[[ID3 | mutagen.FileType, str], list[str]],
) -> str | None:
    """Count each neighboring track once, preferring valid YAML over media tags."""
    counts: Counter[str] = Counter()
    try:
        albums = sorted(path.parent.parent.iterdir())
    except FileNotFoundError:
        logger.info("[%s] Artist directory does not exist; no neighboring genres found", path)
        return None
    for album in albums:
        if not album.is_dir():
            continue
        tracks: dict[str, list[Path]] = {}
        for candidate in sorted(album.iterdir()):
            if (
                not candidate.is_file()
                or candidate.suffix.lower() not in {*MEDIA_EXTENSIONS, ".yml", ".yaml"}
                or candidate.stem.lower() in {"artist", "artists", "album", "files"}
                or (album == path.parent and candidate.stem == path.stem)
            ):
                continue
            tracks.setdefault(candidate.stem, []).append(candidate)
        for candidates in tracks.values():
            genres: list[str] = []
            try:
                value = read_track_values(candidates[0]).get("genre")
                if value:
                    genres = filter_valid_genres([value])
                    if not genres:
                        logger.warning("[%s] Ignoring invalid neighboring YAML genre %r", candidates[0], value)
            except (OSError, UnicodeError, ValueError, TypeError, YAMLError) as error:
                logger.warning("[%s] Could not read neighboring track YAML: %s", candidates[0], error)
            if not genres:
                for candidate in candidates:
                    if candidate.suffix.lower() not in MEDIA_EXTENSIONS:
                        continue
                    try:
                        values = text_values(load_media(candidate), "genre")
                        genres = filter_valid_genres(values)
                        invalid = [value for value in values if value not in genres]
                        if invalid:
                            logger.warning("[%s] Ignoring invalid neighboring media genres %r", candidate, invalid)
                    except (OSError, ValueError, TypeError, mutagen.MutagenError) as error:
                        logger.warning("[%s] Could not read neighboring media genres: %s", candidate, error)
                    if genres:
                        break
            counts.update(set(genres))
    if not counts:
        logger.info("[%s] No valid neighboring genres found for artist", path)
        return None
    selected = min(counts, key=lambda genre: (-counts[genre], genre))
    logger.info("[%s] Inferred artist genre %r from neighboring tracks: %s", path, selected, dict(sorted(counts.items())))
    return selected
