import logging
import random
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from mediascan.types.album.album_path import album_pattern

logger = logging.getLogger(__name__)


def shuffled_files(paths: Iterable[Path]) -> list[Path]:
    files = list(paths)
    random.shuffle(files)
    return files


def path_component(value: str) -> str:
    """Keep directory components compatible with mediascan's album-name grammar."""
    cleaned = re.sub(r'[:?&#%{}\\.`$!<>*"+|=/\[\]\x00-\x1f\x7f]', "_", value).strip()
    if not cleaned or not cleaned.strip("_ "):
        raise ValueError(f"Name cannot form a safe path component: {value!r}")
    if cleaned != value:
        logger.debug("Sanitized path component: %r -> %r", value, cleaned)
    return cleaned


def directory_name_key(value: str) -> str:
    """Compare collection names independently of case and filename escaping."""
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("&", " and ").replace("’", "'").replace("´", "'")
    return re.sub(r"[\W_]+", " ", value).strip()


def existing_directory(parent: Path, name: str, year: int | None = None) -> Path | None:
    if not parent.exists():
        return None
    expected = {directory_name_key(name), directory_name_key(path_component(name))}
    expected.discard("")
    if not expected:
        raise ValueError(f"Name cannot be matched safely to an existing directory: {name!r}")
    matches: list[Path] = []
    for candidate in sorted(parent.iterdir()):
        if not candidate.is_dir():
            continue
        title = candidate.name
        if year is not None:
            dated = re.fullmatch(r"(.*?)\s*\[(\d{4})(?:-(\d{4}))?\]", title)
            if dated is not None:
                if dated.group(3) is not None or int(dated.group(2)) != year:
                    continue
                title = dated.group(1)
        if directory_name_key(title) in expected:
            matches.append(candidate)
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous existing directories for {name!r} under {parent}: "
            + ", ".join(str(path) for path in matches)
        )
    if matches:
        logger.info("Reusing existing directory for %r: %s", name, matches[0])
        return matches[0]
    return None


def album_directory(
    destination: Path, artist: str, album: str, year: int, roots: list[Path] | None = None,
) -> Path:
    artist_name = path_component(artist)
    album_title = path_component(album)
    album_name = f"{album_title} [{year}]"
    if not album_pattern.fullmatch(album_name):
        raise ValueError(f"Invalid mediascan album name: {album_name}")
    artist_dirs = [
        artist_dir for root in (roots if roots is not None else [destination])
        if (artist_dir := existing_directory(root, artist)) is not None
    ]
    albums = [
        album_dir for artist_dir in artist_dirs
        if (album_dir := existing_directory(artist_dir, album, year)) is not None
    ]
    if len(albums) > 1:
        raise ValueError(f"Ambiguous existing album directories for {album!r}: {', '.join(map(str, albums))}")
    if albums:
        return albums[0]
    if len(artist_dirs) > 1:
        raise ValueError(f"Ambiguous existing artist directories for {artist!r}: {', '.join(map(str, artist_dirs))}")
    artist_dir = artist_dirs[0] if artist_dirs else destination / artist_name
    return artist_dir / album_name
