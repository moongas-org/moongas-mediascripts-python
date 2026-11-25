"""Move tracks out of Various Artists using local metadata and MusicBrainz."""

import argparse
import logging
import math
import os
import re
import shutil
import sys
import time
from collections import Counter
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


EDITION_LABEL = re.compile(
    r"(?=.*\b(?:deluxe|edition|remaster(?:ed)?|anniversary|expanded|bonus)\b)"
    r"(?:(?:\d{4}|\d+(?:st|nd|rd|th)?|super|deluxe|special|expanded|extended|"
    r"limited|collector'?s?|collectors'?|anniversary|edition|version|remaster(?:ed)?|"
    r"digitally|digital|bonus|tracks?|disc|with|and|the|of|original|release)"
    r"(?:[\s,/:+&-]+|$))+",
    re.IGNORECASE,
)


def original_album_title(title: str) -> str:
    """Remove only recognizable trailing edition labels, not title parentheses."""
    original = title.strip()
    cleaned = original
    while True:
        suffix = re.search(r"\s*[\(\[]([^\(\)\[\]]+)[\)\]]\s*$", cleaned)
        if suffix is not None and EDITION_LABEL.fullmatch(suffix[1].strip()):
            candidate = cleaned[:suffix.start()].strip()
        else:
            separators = [
                separator for separator in re.finditer(r"\s+[-\u2013\u2014:]\s+", cleaned)
                if EDITION_LABEL.fullmatch(cleaned[separator.end():].strip())
            ]
            if not separators:
                break
            candidate = cleaned[:separators[-1].start()].strip()
        if not candidate:
            break
        cleaned = candidate
    if cleaned != original:
        logger.info("Original album title: %r -> %r (removed edition labels)", original, cleaned)
    return cleaned


def original_album(values: dict[str, str], catalogue: MusicBrainz) -> dict[str, str]:
    """Resolve exact artist/title matches to original official studio album groups."""
    artist_key = directory_name_key(values["artist"])
    title_key = directory_name_key(values["title"])
    year_hint = int(values["year"]) if values["year"] else None
    query = (
        f"artist:{lucene_phrase(values['artist'])} AND recording:{lucene_phrase(values['title'])}"
        " AND primarytype:album AND status:official"
    )
    if year_hint is not None:
        query += f" AND firstreleasedate:[* TO {year_hint}-12-31]"
    logger.info(
        "Searching studio album candidates using embedded releases; no per-recording release requests. "
        "Only distinct eligible album groups missing original metadata require title/year lookups."
    )
    result = catalogue.get(
        "recording",
        query=query,
        limit="100",
    )
    recordings = catalogue_items(result, "recordings")
    count = result.get("count")
    logger.info(
        "Recording search for artist=%r title=%r: returned %d recording(s), total=%s; "
        "source album=%r year hint=%s (compilation album will not be reused)",
        values["artist"], values["title"], len(recordings), count,
        values.get("album", ""), values["year"] or "unknown",
    )
    if type(count) is not int or count > len(recordings):
        raise ValueError("MusicBrainz recording search is incomplete; cannot choose safely")
    groups: dict[str, dict[str, object]] = {}
    group_lookups = 0
    candidates: list[tuple[str, str, str, str]] = []
    rejected_recordings: Counter[str] = Counter()
    examined = 0
    for index, recording in enumerate(recordings, start=1):
        reason = ""
        if directory_name_key(str(recording.get("title", ""))) != title_key:
            reason = "title mismatch"
        elif artist_key not in credited_names(recording):
            reason = "artist credit mismatch or multiple artists"
        elif recording.get("video") is True:
            reason = "video recording"
        # A same-title live/remix/demo recording is not proof of the local version.
        elif recording.get("disambiguation"):
            reason = "disambiguated version"
        if reason:
            rejected_recordings[reason] += 1
            logger.debug(
                "Rejected recording %s: %s; title=%r disambiguation=%r",
                recording.get("id"), reason, recording.get("title"), recording.get("disambiguation"),
            )
            continue
        recording_id = catalogue_id(recording)
        examined += 1
        logger.info(
            "Examining recording %d/%d: id=%s title=%r first-release-date=%s; "
            "filtering embedded releases locally (no request for this recording)",
            index, len(recordings), recording_id, recording.get("title"),
            recording.get("first-release-date", "unknown"),
        )
        releases = catalogue_items(recording, "releases")
        logger.info("Recording %s: %d embedded release(s)", recording_id, len(releases))
        album_options: dict[str, tuple[str, str, str, str]] = {}
        rejected_releases: Counter[str] = Counter()
        for release in releases:
            group = release.get("release-group")
            reason = ""
            if release.get("status") != "Official":
                reason = "not Official"
            elif "artist-credit" in release and artist_key not in credited_names(release):
                reason = "artist credit mismatch or multiple artists"
            elif not is_mapping(group):
                reason = "missing release group"
            elif group.get("primary-type") != "Album":
                reason = "not an Album"
            elif group.get("secondary-types"):
                reason = "secondary release type (e.g. compilation/live/remix)"
            if reason:
                rejected_releases[reason] += 1
                logger.debug(
                    "Rejected release %s title=%r: %s; release-group=%s",
                    release.get("id"), release.get("title"), reason, group,
                )
                continue
            if not is_mapping(group):
                raise ValueError("Expected a release group mapping")
            group_id = catalogue_id(group)
            if group_id not in groups:
                if all(field in group for field in ("title", "first-release-date", "artist-credit")):
                    logger.info("Using embedded original album metadata for release-group=%s", group_id)
                    groups[group_id] = group
                else:
                    logger.info("Looking up eligible studio album release-group=%s title=%r", group_id, group.get("title"))
                    groups[group_id] = catalogue.get(f"release-group/{group_id}", inc="artist-credits")
                    group_lookups += 1
            else:
                logger.debug("Reusing cached release-group=%s", group_id)
            details = groups[group_id]
            if (catalogue_id(details) != group_id or artist_key not in credited_names(details)
                    or details.get("primary-type") != "Album" or details.get("secondary-types")):
                rejected_releases["release-group details not eligible"] += 1
                continue
            date = details.get("first-release-date")
            title = details.get("title")
            if (not isinstance(date, str) or not re.fullmatch(r"[1-9]\d{3}(?:-\d{2}(?:-\d{2})?)?", date)
                    or not isinstance(title, str) or not title.strip()):
                rejected_releases["missing/invalid album title or release date"] += 1
                continue
            if year_hint is not None and int(date[:4]) > year_hint:
                rejected_releases["album newer than source year hint"] += 1
                continue
            title = original_album_title(title)
            if group_id not in album_options:
                logger.info("Eligible studio album: %r [%s] release-group=%s", title, date[:4], group_id)
            album_options[group_id] = (date, title, group_id, recording_id)
        logger.info(
            "Recording %s release filtering: %d release(s), %d eligible studio album group(s); rejected=%s",
            recording_id, len(releases), len(album_options), dict(rejected_releases),
        )
        if album_options:
            earliest = min(option[0][:4] for option in album_options.values())
            earliest_options = [option for option in album_options.values() if option[0][:4] == earliest]
            candidates.extend(earliest_options)
            logger.info(
                "Recording %s earliest studio album year=%s; retained=%s",
                recording_id, earliest, [option[1] for option in earliest_options],
            )
    by_group = {option[2]: option for option in candidates}
    logger.info(
        "Recording search summary: %d examined locally, %d rejected (%s); "
        "%d distinct album group lookup(s); %d distinct earliest studio album group(s)",
        examined, sum(rejected_recordings.values()), dict(rejected_recordings), group_lookups, len(by_group),
    )
    # Compilation/reissue years must not override an earlier original album.
    earliest_year = min((option[0][:4] for option in by_group.values()), default=None)
    oldest = [option for option in by_group.values() if option[0][:4] == earliest_year]
    by_title: dict[str, tuple[str, str, str, str]] = {}
    for option in sorted(oldest):
        by_title.setdefault(directory_name_key(option[1]), option)
    logger.info(
        "Oldest album selection: year=%s; %d group(s) reduce to %d original title(s); "
        "source year is an upper bound, not a preference for a reissue",
        earliest_year or "unknown", len(oldest), len(by_title),
    )
    if len(by_title) != 1:
        logger.warning(
            "Unresolved album candidates: %s",
            [{"album": option[1], "date": option[0], "release-group": option[2]} for option in by_title.values()],
        )
        raise ValueError(f"No unambiguous original studio album found ({len(by_title)} candidates)")
    date, album, group_id, recording_id = next(iter(by_title.values()))
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
    if (album_data.get("name")
            and directory_name_key(original_album_title(str(album_data["name"]))) != directory_name_key(values["album"])):
        raise ValueError(f"Destination album YAML names a different album: {album_yaml}")
    album_data["name"] = values["album"]
    release_key = "date_released" if "date_released" in album_data else "dateReleased"
    release_date = album_data.get(release_key)
    if release_date is None:
        album_data[release_key] = {"y": int(values["year"])}
    elif not is_mapping(release_date):
        raise ValueError(f"Destination album YAML has an invalid release date: {album_yaml}")
    elif release_date.get("y") != int(values["year"]):
        logger.info(
            "[%s] Correcting album release year from %s to original year %s; discarding edition-specific month/day",
            album_yaml, release_date.get("y"), values["year"],
        )
        album_data[release_key] = {"y": int(values["year"])}
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
    logger.info(
        "Move settings: root=%s dry_run=%s count=%s sleep=%.2fs; "
        "count limits successful tracks, not MusicBrainz requests; dry-run still performs online lookups",
        data_root, dry_run, count if count is not None else "unlimited", sleep,
    )
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
    logger.info("Found %d Various Artists source directory(s): %s", len(sources), [str(source) for source in sources])
    catalogue = MusicBrainz(sleep=sleep)
    processed = 0
    attempted = 0
    skipped = 0
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
            attempted += 1
            started = time.perf_counter()
            logger.info(
                "[%s] Starting track %d; completed=%d/%s skipped=%d",
                path, attempted, processed, count if count is not None else "unlimited", skipped,
            )
            try:
                move_track(path, data_root, catalogue, artist_root=source.parent, dry_run=dry_run)
            except (ValueError, YAMLError, UnicodeError, FileExistsError) as error:
                skipped += 1
                logger.warning(
                    "[%s] Skipping track after %.2fs: %s", path, time.perf_counter() - started, error,
                )
                continue
            except OSError:
                logger.error(
                    "[%s] Track processing failed after %.2fs; stopping run "
                    "(completed=%d skipped=%d dry_run=%s)",
                    path, time.perf_counter() - started, processed, skipped, dry_run,
                )
                raise
            processed += 1
            logger.info(
                "[%s] %s track in %.2fs; completed=%d skipped=%d",
                path, "Previewed" if dry_run else "Moved", time.perf_counter() - started, processed, skipped,
            )
            if count is not None and processed >= count:
                logger.info("Track limit reached; stopping (attempted=%d completed=%d skipped=%d)", attempted, processed, skipped)
                return True
    if processed == 0:
        logger.info("No safely identifiable, non-colliding track found")
    logger.info("Run complete: attempted=%d completed=%d skipped=%d dry_run=%s", attempted, processed, skipped, dry_run)
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
    parser.add_argument("-v", "--verbose", action="store_true", help="Include DEBUG logs and raw MusicBrainz responses.")
    args = parser.parse_args()
    if args.count < 0:
        parser.error("--count must be non-negative")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    )
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