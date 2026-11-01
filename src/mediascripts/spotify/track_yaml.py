"""Validate track identifiers, collect unique ISRCs, and update Spotify YAML data."""

import logging
from collections.abc import Mapping
from pathlib import Path

from mediascripts.acoustid.recording_yaml import is_list, is_mapping

logger = logging.getLogger(__name__)


def identifier_entries(
    document: dict[str, object], path: Path
) -> list[tuple[dict[str, object], list[tuple[str, str]]]]:
    identifiers_value = document.get("trackIdData")
    if identifiers_value is None:
        identifiers: list[dict[str, object]] = []
    elif is_mapping(identifiers_value):
        identifiers = [identifiers_value]
    else:
        raise ValueError("trackIdData must be a mapping")
    entries: list[tuple[dict[str, object], list[tuple[str, str]]]] = []
    for entry in identifiers:
        if entry.keys() - {"streamingData"}:
            raise ValueError("trackIdData contains unsupported fields")
        isrcs: list[tuple[str, str]] = []

        def add(value: object) -> None:
            if value is None:
                return
            if not isinstance(value, str):
                raise ValueError("ISRC must be a string")
            normalized = value.strip().replace("-", "").upper()
            if normalized and all(candidate != normalized for candidate, _ in isrcs):
                isrcs.append((normalized, value))

        streaming_data = entry.get("streamingData")
        if streaming_data is not None:
            if not is_list(streaming_data):
                raise ValueError("streamingData must be a list")
            for streaming_index, streaming in enumerate(streaming_data):
                if not is_mapping(streaming):
                    raise ValueError("streamingData entries must be mappings")
                field_path = f"trackIdData.streamingData[{streaming_index}]"
                if streaming.keys() - {"streamingService", "trackUrl", "trackId", "apiData", "isrc"}:
                    raise ValueError(f"{field_path} contains unsupported fields")
                for required in ("streamingService", "trackUrl", "trackId"):
                    value = streaming.get(required)
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(f"{field_path}.{required} must be a non-empty string")
                if "apiData" not in streaming:
                    raise ValueError(f"{field_path}.apiData is required")
                if streaming.get("isrc") is not None and not isinstance(streaming["isrc"], str):
                    raise ValueError(f"{field_path}.isrc must be a string or null")
                add(streaming.get("isrc"))
        entries.append((entry, isrcs))
        if not isrcs:
            logger.debug("[%s] trackIdData: no streaming ISRC", path)

    media_files = document.get("mediaFileData")
    media_isrcs: list[tuple[str, str]] = []
    if media_files is not None:
        if not is_list(media_files):
            raise ValueError("mediaFileData must be a list")
        for media_file in media_files:
            if not is_mapping(media_file):
                raise ValueError("mediaFileData entries must be mappings")
            data = media_file.get("acoustidData")
            if data is None:
                continue
            if not is_mapping(data):
                raise ValueError("mediaFileData acoustidData must be a mapping")
            results_value: object = data.get("results", [])
            if not is_list(results_value):
                raise ValueError("mediaFileData acoustidData must have a results list")
            for result in results_value:
                if not is_mapping(result):
                    raise ValueError("AcoustID results must be mappings")
                recordings_value: object = result.get("recordings", [])
                if not is_list(recordings_value):
                    raise ValueError("AcoustID results must have recording lists")
                for recording in recordings_value:
                    if not is_mapping(recording):
                        raise ValueError("AcoustID recordings must be mappings")
                    values = recording.get("isrcs")
                    if values is None:
                        continue
                    if not is_list(values):
                        raise ValueError("Recording ISRCs must be a list")
                    for value in values:
                        if value is None:
                            continue
                        if not isinstance(value, str):
                            raise ValueError("ISRC must be a string")
                        normalized = value.strip().replace("-", "").upper()
                        if normalized and all(candidate != normalized for candidate, _ in media_isrcs):
                            media_isrcs.append((normalized, value))
    known_isrcs = {
        isrc for _, entry_isrcs in entries for isrc, _ in entry_isrcs
    }
    new_media_isrcs = [item for item in media_isrcs if item[0] not in known_isrcs]
    if new_media_isrcs:
        target_isrcs = next(
            (isrcs for entry, isrcs in entries if not has_spotify_data(entry)),
            entries[0][1] if entries else None,
        )
        if target_isrcs is None:
            entries.append(({}, new_media_isrcs))
        else:
            target_isrcs.extend(
                item for item in new_media_isrcs
                if all(existing != item[0] for existing, _ in target_isrcs)
            )
    for _, isrcs in entries:
        isrcs.sort(key=lambda item: item[0], reverse=True)
    return [entry for entry in entries if entry[1]]


def has_spotify_data(entry: Mapping[str, object]) -> bool:
    streaming_data = entry.get("streamingData")
    if not is_list(streaming_data):
        return False
    for item in streaming_data:
        if not is_mapping(item) or item.get("streamingService") != "spotify":
            continue
        track_id = item.get("trackId")
        track_url = item.get("trackUrl")
        if (
            isinstance(track_id, str)
            and track_id.strip()
            and isinstance(track_url, str)
            and track_url.strip()
        ):
            return True
    return False


def store_spotify_data(
    document: dict[str, object],
    entry: dict[str, object],
    isrc: str,
    track_id: str,
    track_url: str,
    api_data: dict[str, object],
) -> bool:
    streaming_data_value = entry.get("streamingData")
    if streaming_data_value is None:
        streaming_data: list[object] = []
        entry["streamingData"] = streaming_data
    elif is_list(streaming_data_value):
        streaming_data = streaming_data_value
    else:
        raise ValueError("streamingData must be a list")
    spotify_entries = [
        item for item in streaming_data
        if is_mapping(item) and item.get("streamingService") == "spotify"
    ]
    if spotify_entries:
        spotify_entry = spotify_entries[0]
        changed = (
            spotify_entry.get("trackId") != track_id
            or spotify_entry.get("trackUrl") != track_url
            or spotify_entry.get("apiData") != api_data
        )
        spotify_entry["trackId"] = track_id
        spotify_entry["trackUrl"] = track_url
        spotify_entry["apiData"] = api_data
        if not spotify_entry.get("isrc"):
            spotify_entry["isrc"] = isrc
            changed = True
    else:
        streaming_data.append({
            "streamingService": "spotify",
            "trackUrl": track_url,
            "trackId": track_id,
            "apiData": api_data,
            "isrc": isrc,
        })
        changed = True
    if changed:
        identifiers_value = document.get("trackIdData")
        if identifiers_value is None:
            document["trackIdData"] = entry
        elif not is_mapping(identifiers_value):
            raise ValueError("trackIdData must be a mapping")
        elif identifiers_value is not entry:
            document["trackIdData"] = entry
    return changed
