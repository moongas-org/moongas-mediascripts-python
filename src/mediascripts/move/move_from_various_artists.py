"""Move tracks out of Various Artists using local metadata and MusicBrainz."""

import argparse
import logging
import math
import os
import re
import shutil
import sys
from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from io import StringIO
from pathlib import Path
from typing import Protocol, cast

import mutagen
from mediascan.types.track.track_yaml_file_validator import is_list, is_mapping
from ruamel.yaml.error import YAMLError

from mediascripts.acoustid.musicbrainz import MIN_SLEEP, is_recording_id, sleep_seconds
from mediascripts.common.files import album_directory, directory_name_key
from mediascripts.common.musicbrainz import MusicBrainz, lucene_phrase
from mediascripts.lyrics.lyrics_common import MEDIA_EXTENSIONS
from mediascripts.mediafile.auto_tag_media_file import load_media, read_track_values, text_values
from mediascripts.track.track_yaml.track_yaml_common import (
    TextWriter,
    iter_collection_files,
    report_walk_error,
    save_yaml_atomically,
    show_yaml_file_diff,
    track_yaml,
)

logger = logging.getLogger(__name__)
YAML_SUFFIXES = {".yml", ".yaml"}
RESERVED_STEMS = {"album", "artist", "artists", "files"}
UNKNOWN_ARTISTS = {"various artists", "various", "va", "v a", "unknown", "unknown artist"}


class _Yaml(Protocol):
    def load(self, stream: Path) -> object: ...
    def dump(self, data: Mapping[str, object], stream: TextWriter) -> None: ...


def yaml_document(path: Path | None) -> MutableMapping[str, object]:
    if path is None:
        return {}
    document = cast(_Yaml, track_yaml(preserve_quotes=True)).load(path)
    if document is None:
        logger.info("[%s] Initializing empty YAML", path)
        return {}
    if not is_mapping(document):
        raise ValueError(f"Expected a YAML mapping: {path}")
    return document


def metadata(document: MutableMapping[str, object], key: str, alias: str) -> MutableMapping[str, object]:
    if key in document and alias in document:
        raise ValueError(f"Ambiguous YAML: both {key} and {alias} are present")
    selected = alias if alias in document else key
    if selected not in document:
        document[selected] = {}
    data = document[selected]
    if not is_mapping(data):
        raise ValueError(f"Expected a {selected} mapping")
    return data


def yaml_sidecar(directory: Path, stem: str) -> Path | None:
    candidates = [
        path for path in directory.iterdir()
        if path.stem.casefold() == stem.casefold() and path.suffix.lower() in YAML_SUFFIXES
    ] if directory.exists() else []
    if len(candidates) > 1:
        raise ValueError(f"Multiple YAML files for {directory / stem}: {candidates}")
    if candidates and not candidates[0].is_file():
        raise ValueError(f"YAML path is not a file: {candidates[0]}")
    return candidates[0] if candidates else None


def known_artist(value: str) -> bool:
    key = directory_name_key(value)
    return bool(key) and key not in UNKNOWN_ARTISTS


def identify_track(path: Path, files: list[Path], values: dict[str, str]) -> dict[str, str]:
    artists = [values["artist"]] if known_artist(values.get("artist", "")) else []
    tags: dict[str, str] = {}
    for media_path in files:
        if media_path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        try:
            media = load_media(media_path)
        except (ValueError, mutagen.MutagenError) as error:
            logger.warning("[%s] Could not read embedded tags: %s", media_path, error)
            continue
        for field in ("artist", "album", "title", "genre", "year"):
            candidates = [value.strip() for value in text_values(media, field) if value.strip()]
            if field == "artist":
                artists.extend(value for value in candidates if known_artist(value))
            if candidates:
                tags.setdefault(field, candidates[0])
    stem = re.sub(r"^\d+(?:[.-]\d+)?\s*[-.]\s*|^\d+\s+", "", path.stem).strip()
    prefix, separator, title = stem.partition(" - ")
    if not artists:
        for label in (stem, values.get("title", "")):
            inferred_artist, delimiter, inferred_title = label.partition(" - ")
            if delimiter and known_artist(inferred_artist) and inferred_title.strip():
                artists.append(inferred_artist.strip())
                logger.info("[%s] Inferring artist from artist/title prefix: %s", path, inferred_artist)
    keys = {directory_name_key(artist) for artist in artists}
    if len(keys) != 1:
        raise ValueError(f"Artist is unknown or conflicting: {artists}")
    artist = artists[0]
    values = {**tags, **values, "artist": artist}
    if separator and directory_name_key(prefix) == directory_name_key(artist):
        stem = title.strip()
    values.setdefault("title", stem)
    title_artist, delimiter, title_text = values["title"].partition(" - ")
    if delimiter and directory_name_key(title_artist) == directory_name_key(artist):
        values["title"] = title_text.strip()
    source_album = re.fullmatch(r"(.*?)\s*\[(\d{4})\]", path.parent.name)
    source_document = yaml_document(yaml_sidecar(path.parent, "album"))
    source_data = metadata(source_document, "albumData", "album_data")
    values.setdefault("album", str(source_data.get("name") or (source_album[1] if source_album else "")))
    release = source_data.get("dateReleased", source_data.get("date_released"))
    release_year = release.get("y") if is_mapping(release) else None
    values.setdefault("year", str(release_year or (source_album[2] if source_album else "")))
    year = re.fullmatch(r"([1-9]\d{3})(?:-\d{2}(?:-\d{2})?)?", values.get("year", ""))
    if not values["title"].strip():
        raise ValueError("A track title is required")
    if values["year"] and year is None:
        raise ValueError(f"Invalid track year: {values['year']!r}")
    values["year"] = year[1] if year else ""
    return values


def credited_names(record: Mapping[str, object]) -> set[str]:
    credits = record.get("artist-credit")
    if not is_list(credits) or len(credits) != 1 or not is_mapping(credits[0]):
        return set()
    credit = credits[0]
    names = [credit.get("name")]
    artist = credit.get("artist")
    if is_mapping(artist):
        names.append(artist.get("name"))
    return {directory_name_key(name) for name in names if isinstance(name, str)}


def catalogue_items(response: Mapping[str, object], key: str) -> list[dict[str, object]]:
    items = response.get(key)
    if not is_list(items) or not all(is_mapping(item) for item in items):
        raise ValueError(f"Invalid MusicBrainz {key} response")
    return cast(list[dict[str, object]], items)


def catalogue_id(record: Mapping[str, object]) -> str:
    identifier = record.get("id")
    if not isinstance(identifier, str) or not is_recording_id(identifier):
        raise ValueError("Missing or invalid MusicBrainz identifier")
    return identifier


def original_album(values: dict[str, str], catalogue: MusicBrainz) -> dict[str, str]:
    """Resolve exact artist/title matches to original official studio album groups."""
    artist_key = directory_name_key(values["artist"])
    title_key = directory_name_key(values["title"])
    year_hint = int(values["year"]) if values["year"] else None
    result = catalogue.get(
        "recording",
        query=f"artist:{lucene_phrase(values['artist'])} AND recording:{lucene_phrase(values['title'])}",
        limit="100",
    )
    recordings = catalogue_items(result, "recordings")
    count = result.get("count")
    if type(count) is not int or count > len(recordings):
        raise ValueError("MusicBrainz recording search is incomplete; cannot choose safely")
    groups: dict[str, dict[str, object]] = {}
    candidates: list[tuple[str, str, str, str]] = []
    for recording in recordings:
        if (directory_name_key(str(recording.get("title", ""))) != title_key
                or artist_key not in credited_names(recording) or recording.get("video") is True):
            continue
        # A same-title live/remix/demo recording is not proof of the local version.
        if recording.get("disambiguation"):
            continue
        recording_id = catalogue_id(recording)
        releases: list[dict[str, object]] = []
        for offset in range(0, 1000, 100):
            page = catalogue.get(
                "release", recording=recording_id, inc="artist-credits+release-groups",
                limit="100", offset=str(offset),
            )
            batch = catalogue_items(page, "releases")
            releases.extend(batch)
            total = page.get("release-count")
            if type(total) is not int:
                raise ValueError("MusicBrainz release count is missing")
            if len(releases) >= total:
                break
            if not batch:
                raise ValueError("MusicBrainz release listing ended before its reported count")
        else:
            raise ValueError("Too many releases to resolve this recording safely")
        album_options: dict[str, tuple[str, str, str, str]] = {}
        for release in releases:
            group = release.get("release-group")
            if (release.get("status") != "Official" or artist_key not in credited_names(release)
                    or not is_mapping(group) or group.get("primary-type") != "Album"
                    or group.get("secondary-types")):
                continue
            group_id = catalogue_id(group)
            if group_id not in groups:
                groups[group_id] = catalogue.get(f"release-group/{group_id}", inc="artist-credits")
            details = groups[group_id]
            if (catalogue_id(details) != group_id or artist_key not in credited_names(details)
                    or details.get("primary-type") != "Album" or details.get("secondary-types")):
                continue
            date = details.get("first-release-date")
            title = details.get("title")
            if (not isinstance(date, str) or not re.fullmatch(r"[1-9]\d{3}(?:-\d{2}(?:-\d{2})?)?", date)
                    or not isinstance(title, str) or not title.strip()):
                continue
            if year_hint is not None and int(date[:4]) > year_hint:
                continue
            album_options[group_id] = (date, title.strip(), group_id, recording_id)
        if album_options:
            earliest = min(option[0][:4] for option in album_options.values())
            candidates.extend(option for option in album_options.values() if option[0][:4] == earliest)
    # Distinct recordings with different source albums need a year match to disambiguate.
    by_group = {option[2]: option for option in candidates}
    if len(by_group) > 1 and year_hint is not None:
        exact_year = {key: option for key, option in by_group.items() if int(option[0][:4]) == year_hint}
        if exact_year:
            by_group = exact_year
    if len(by_group) != 1:
        raise ValueError(f"No unambiguous original studio album found ({len(by_group)} candidates)")
    date, album, group_id, recording_id = next(iter(by_group.values()))
    logger.info(
        "Resolved %s - %s (source year %s) to %s [%s]; MusicBrainz recording=%s release-group=%s",
        values["artist"], values["title"], values["year"] or "unknown", album, date[:4],
        recording_id, group_id,
    )
    return {**values, "album": album, "year": date[:4], "albumartist": values["artist"]}


def serialize(document: MutableMapping[str, object]) -> str:
    stream = StringIO()
    cast(_Yaml, track_yaml(preserve_quotes=True)).dump(document, stream)
    return stream.getvalue()


def rewrite_paths(node: object, replacements: dict[str, str]) -> None:
    if is_mapping(node):
        for key, value in node.items():
            if isinstance(value, str):
                for original, updated in replacements.items():
                    if value == original or value.startswith(original + "/"):
                        node[key] = updated + value[len(original):]
                        break
            else:
                rewrite_paths(value, replacements)
    elif isinstance(node, list):
        for value in cast(list[object], node):
            rewrite_paths(value, replacements)


def write_yaml(path: Path, content: str, *, create_only: bool = False) -> None:
    def write(stream: TextWriter) -> None:
        stream.write(content)

    save_yaml_atomically(path, write, create_only=create_only)


def copy_exclusively(source: Path, destination: Path) -> None:
    output = destination.open("xb")
    try:
        with output:
            with source.open("rb") as stream:
                shutil.copyfileobj(stream, output)
        shutil.copystat(source, destination)
    except (OSError, KeyboardInterrupt):
        destination.unlink()
        raise


def move_track(
    path: Path, data_root: Path, catalogue: MusicBrainz, *, artist_root: Path, dry_run: bool = False,
) -> Path:
    source_yaml = yaml_sidecar(path.parent, path.stem)
    files = sorted(
        candidate for candidate in path.parent.iterdir()
        if candidate.is_file() and candidate.stem == path.stem and candidate != source_yaml
    )
    if source_yaml is not None and source_yaml.stem != path.stem:
        raise ValueError(f"Track YAML basename differs in case from media: {source_yaml}")
    source_yaml_original = source_yaml.read_bytes() if source_yaml is not None else None
    document = yaml_document(source_yaml)
    initial_values = read_track_values(path) if document else {}
    data = metadata(document, "trackData", "track_data")
    values = original_album(identify_track(path, files, initial_values), catalogue)
    destination = album_directory(artist_root, values["artist"], values["album"], int(values["year"]))
    if any(directory_name_key(parent.name) == "various artists" for parent in destination.parents
           if parent != data_root and parent.is_relative_to(data_root)):
        raise ValueError("Destination is still inside Various Artists")
    target_yaml = destination / (source_yaml.name if source_yaml else f"{path.stem}.yml")
    targets = [destination / file.name for file in files] + [target_yaml]
    existing = list(destination.iterdir()) if destination.exists() else []
    for entry in existing:
        if (entry.name.casefold() in {target.name.casefold() for target in targets}
                or directory_name_key(entry.stem) == directory_name_key(path.stem)):
            raise FileExistsError(f"Destination track or sidecar already exists: {entry}")

    for field in ("artist", "albumartist", "album", "title", "genre"):
        if field in values:
            data[field] = values[field]
    data["year"] = int(values["year"])
    if not data.get("genre"):
        logger.warning("[%s] Genre unavailable; recording genre as Unknown", path)
        data["genre"] = "Unknown"
    replacements: dict[str, str] = {
        str(path.parent.absolute()): str(destination.absolute()),
        "/" + path.parent.relative_to(data_root.parent).as_posix(): (
            "/" + destination.relative_to(data_root.parent).as_posix()
        ),
    }
    for source in files:
        target = destination / source.name
        replacements[str(source.absolute())] = str(target.absolute())
        replacements["/" + source.relative_to(data_root.parent).as_posix()] = (
            "/" + target.relative_to(data_root.parent).as_posix()
        )
    rewrite_paths(document, replacements)
    track_content = serialize(document)

    album_yaml = yaml_sidecar(destination, "album") or destination / "album.yml"
    album_original = album_yaml.read_text(encoding="utf-8") if album_yaml.exists() else None
    album_document = yaml_document(album_yaml if album_original is not None else None)
    album_before = deepcopy(album_document)
    album_data = metadata(album_document, "albumData", "album_data")
    if album_data.get("name") and directory_name_key(str(album_data["name"])) != directory_name_key(values["album"]):
        raise ValueError(f"Destination album YAML names a different album: {album_yaml}")
    if not album_data.get("name"):
        album_data["name"] = values["album"]
    release_key = "date_released" if "date_released" in album_data else "dateReleased"
    release_date = album_data.get(release_key)
    if release_date is None:
        album_data[release_key] = {"y": int(values["year"])}
    elif not is_mapping(release_date) or release_date.get("y") != int(values["year"]):
        raise ValueError(f"Destination album YAML has a conflicting release year: {album_yaml}")
    if "explicitCover" not in album_data and "explicit_cover" not in album_data:
        album_data["explicitCover"] = False
    album_content = (
        album_original if album_original is not None and album_document == album_before
        else serialize(album_document)
    )

    logger.info("%s%s -> %s", "[dry-run] " if dry_run else "", path, destination)
    show_yaml_file_diff(target_yaml, "", track_content)
    if album_content != album_original:
        show_yaml_file_diff(album_yaml, album_original or "", album_content)
    if dry_run:
        return target_yaml

    created_directories: list[Path] = []
    published: list[Path] = []
    removed: list[Path] = []
    album_saved = False
    try:
        for directory in (destination.parent, destination):
            if not directory.exists():
                directory.mkdir()
                created_directories.append(directory)
        for source in files:
            target = destination / source.name
            copy_exclusively(source, target)
            published.append(target)
        write_yaml(target_yaml, track_content, create_only=True)
        published.append(target_yaml)
        if album_content != album_original:
            write_yaml(album_yaml, album_content, create_only=album_original is None)
            album_saved = True
        # Keep every original until all destination files and metadata are complete.
        for source in files:
            removed.append(source)
            source.unlink()
        if source_yaml is not None:
            source_yaml.unlink()
    except (OSError, KeyboardInterrupt):
        logger.error("Move failed; restoring source files and destination metadata")
        for source in removed:
            if not source.exists():
                copy_exclusively(destination / source.name, source)
        if source_yaml is not None and source_yaml_original is not None and not source_yaml.exists():
            with source_yaml.open("xb") as stream:
                stream.write(source_yaml_original)
        if album_saved:
            if album_original is None:
                album_yaml.unlink()
            else:
                write_yaml(album_yaml, album_original)
        for target in reversed(published):
            target.unlink()
        for directory in reversed(created_directories):
            directory.rmdir()
        raise
    return target_yaml


def move_from_various_artists(
    data_root: Path, *, dry_run: bool = False, count: int | None = 1, sleep: float = MIN_SLEEP,
) -> bool:
    """Skip uncertain/colliding tracks and limit successful moves or previews."""
    if count is not None and count < 0:
        raise ValueError("count must be non-negative")
    if not math.isfinite(sleep) or sleep < MIN_SLEEP:
        raise ValueError(f"sleep must be a finite number of seconds >= {MIN_SLEEP:g}")
    if count == 0:
        logger.info("Track limit is zero; nothing to process")
        return False
    data_root = data_root.absolute()
    if not data_root.is_dir():
        raise NotADirectoryError(data_root)
    sources: list[Path] = []
    visited: set[Path] = set()
    for root, directories, _ in os.walk(data_root, followlinks=True, onerror=report_walk_error):
        directory = Path(root)
        resolved = directory.resolve()
        if resolved in visited:
            directories.clear()
            continue
        visited.add(resolved)
        if directory_name_key(directory.name) == "various artists":
            sources.append(directory)
            directories.clear()
        else:
            directories.sort()
    if not sources:
        raise FileNotFoundError(f"No Various Artists directory under {data_root}")
    seen: set[tuple[Path, str]] = set()
    catalogue = MusicBrainz(sleep=sleep)
    processed = 0
    for source in sources:
        for path in iter_collection_files(source, album_directories_only=False):
            if path.suffix.lower() not in (*MEDIA_EXTENSIONS, *YAML_SUFFIXES):
                continue
            if path.stem.casefold() in RESERVED_STEMS:
                continue
            key = (path.parent, path.stem)
            if key in seen:
                continue
            seen.add(key)
            try:
                move_track(path, data_root, catalogue, artist_root=source.parent, dry_run=dry_run)
            except (ValueError, YAMLError, UnicodeError, FileExistsError) as error:
                logger.warning("[%s] Skipping track: %s", path, error)
                continue
            processed += 1
            logger.info("%s %d track(s)", "Previewed" if dry_run else "Moved", processed)
            if count is not None and processed >= count:
                logger.info("Track limit reached; stopping")
                return True
    if processed == 0:
        logger.info("No safely identifiable, non-colliding track found")
    return processed > 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-d", "--dir", type=Path, help="Collection data directory to search recursively.")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Preview moves without changing any files.")
    parser.add_argument(
        "-c", "--count", type=int, default=1,
        help="Maximum successful moves or previews (non-negative; default: 1).",
    )
    parser.add_argument(
        "--sleep", type=sleep_seconds, default=MIN_SLEEP,
        help=f"Minimum seconds between API requests (default and minimum: {MIN_SLEEP:g}).",
    )
    args = parser.parse_args()
    if args.count < 0:
        parser.error("--count must be non-negative")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    directory = args.dir
    if directory is None:
        root = os.environ.get("MOONGAS_COLLECTION_ROOTDIR")
        if not root:
            parser.error("Set MOONGAS_COLLECTION_ROOTDIR or supply --dir")
        directory = Path(root) / "data"
    try:
        move_from_various_artists(directory, dry_run=args.dry_run, count=args.count, sleep=args.sleep)
    except (OSError, ValueError, YAMLError, UnicodeError) as error:
        logger.error("Could not move track: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())