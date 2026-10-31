"""Add Spotify streaming data to track YAML using streaming and recording ISRCs."""

import argparse
import json
import logging
import math
import os
import shlex
import tempfile
import time
from collections.abc import Mapping
from copy import deepcopy
from io import StringIO
from pathlib import Path
from typing import Protocol, TypedDict, cast
from urllib.parse import urlencode

import requests
from ruamel.yaml import YAML

from mediascripts.acoustid.acoustid import YAML_ERRORS, redact, track_files
from mediascripts.acoustid.recording_yaml import is_list, is_mapping
from mediascripts.track.track_yaml.track_yaml_common import TextWriter, save_yaml_atomically

logger = logging.getLogger(__name__)
TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
MIN_SLEEP = 1.01
MAX_ATTEMPTS = 5
USER_AGENT = "moongas-mediascripts/1.2.1"


class _Yaml(Protocol):
    def load(self, stream: str) -> object: ...
    def dump(self, data: Mapping[str, object], stream: TextWriter) -> None: ...


class SpotifyExternalUrls(TypedDict):
    spotify: str


class SpotifyTrack(TypedDict):
    name: str
    id: str
    external_urls: SpotifyExternalUrls
    api_data: dict[str, object]


class SpotifyHTTPError(ValueError):
    def __init__(self, status: int, retry_after: float = 0) -> None:
        super().__init__(f"Spotify returned HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def search_params(isrc: str) -> dict[str, str]:
    return {"q": f"isrc:{isrc}", "type": "track"}


def search_url(isrc: str) -> str:
    return f"{SEARCH_URL}?{urlencode(search_params(isrc))}"


def retry_after_seconds(response: requests.Response) -> float:
    if response.status_code != 429:
        return 0
    try:
        seconds = float(response.headers.get("Retry-After", "0"))
    except ValueError:
        logger.warning("Invalid Spotify Retry-After header; using normal request spacing")
        return 0
    if not math.isfinite(seconds) or seconds < 0:
        logger.warning("Invalid Spotify Retry-After header; using normal request spacing")
        return 0
    return seconds


def response_payload(response: requests.Response, secrets: tuple[str, ...]) -> object:
    def safe(text: str) -> str:
        for secret in secrets:
            text = redact(text, secret)
        return text

    logger.info("Spotify response: HTTP %d", response.status_code)
    try:
        payload: object = response.json()
    except ValueError:
        logger.warning("Spotify response is not valid JSON; raw body:\n%s", safe(response.text))
        if response.status_code == 200:
            raise ValueError("Spotify response is not valid JSON") from None
        payload = None
    else:
        logger.info("Spotify raw response JSON:\n%s", safe(json.dumps(payload, indent=2)))
    if response.status_code != 200:
        raise SpotifyHTTPError(response.status_code, retry_after_seconds(response))
    return payload


def get_access_token(client_id: str, client_secret: str) -> str:
    logger.info("Spotify request: POST %s credentials=[REDACTED] timeout=30s", TOKEN_URL)
    response = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        timeout=30,
    )
    # Never log a token response body, including malformed or error responses.
    logger.info("Spotify authentication response: HTTP %d (body redacted)", response.status_code)
    if response.status_code != 200:
        raise SpotifyHTTPError(response.status_code, retry_after_seconds(response))
    try:
        payload: object = response.json()
    except ValueError:
        raise ValueError("Spotify authentication response is not valid JSON (body redacted)") from None
    if not is_mapping(payload):
        raise ValueError("Spotify authentication response must be a JSON object")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError("Spotify authentication response has no access token")
    return access_token


def get_track_by_isrc(isrc: str, access_token: str) -> list[SpotifyTrack]:
    logger.info(
        "Spotify request: GET %s headers={Authorization: [REDACTED]} timeout=30s", search_url(isrc)
    )
    response = requests.get(
        SEARCH_URL,
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT},
        params=search_params(isrc),
        timeout=30,
    )
    payload = response_payload(response, (access_token,))
    if not is_mapping(payload):
        raise ValueError("Spotify search response must be a JSON object")
    tracks = payload.get("tracks")
    if not is_mapping(tracks):
        raise ValueError("Spotify search response has no tracks object")
    items = tracks.get("items")
    if not is_list(items):
        raise ValueError("Spotify search response tracks.items must be a list")
    result: list[SpotifyTrack] = []
    for item in items:
        if not is_mapping(item):
            raise ValueError("Spotify search result must be an object")
        name, track_id, external_urls = item.get("name"), item.get("id"), item.get("external_urls")
        spotify_url = external_urls.get("spotify") if is_mapping(external_urls) else None
        if not isinstance(name, str) or not isinstance(track_id, str) or not track_id.strip():
            raise ValueError("Spotify track must have a string name and non-empty string ID")
        if not isinstance(spotify_url, str) or spotify_url != f"https://open.spotify.com/track/{track_id}":
            raise ValueError("Spotify track must have a Spotify track URL matching its ID")
        result.append({
            "name": name,
            "id": track_id,
            "external_urls": {"spotify": spotify_url},
            "api_data": payload,
        })
    return result


class SpotifyClient:
    """Space all requests, refresh expired tokens, and cache successes and failures per ISRC."""

    def __init__(self, client_id: str, client_secret: str, sleep: float) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.sleep = sleep
        self.token = ""
        self.secrets = [client_id, client_secret]
        self.last_request: float | None = None
        self.retry_until = 0.0
        self.extra_sleep = 0.0
        self.cache: dict[str, list[SpotifyTrack]] = {}
        self.failed: set[str] = set()

    def safe(self, text: str) -> str:
        for secret in self.secrets:
            text = redact(text, secret)
        return text

    def wait(self) -> None:
        now = time.monotonic()
        deadline = self.retry_until
        if self.last_request is not None:
            deadline = max(deadline, self.last_request + self.sleep + self.extra_sleep)
        delay = max(0, deadline - now)
        if delay:
            logger.info("Sleeping %.3f second(s) before next Spotify request", delay)
            time.sleep(delay)
        self.last_request = time.monotonic()

    def lookup(self, isrc: str) -> list[SpotifyTrack]:
        if isrc in self.failed:
            raise ValueError(f"Spotify lookup for {isrc} already failed this run")
        if isrc in self.cache:
            logger.info("Using cached Spotify result for %s", isrc)
            return self.cache[isrc]
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if not self.token:
                    self.wait()
                    self.token = get_access_token(self.client_id, self.client_secret)
                    self.secrets.append(self.token)
                self.wait()
                result = get_track_by_isrc(isrc, self.token)
                self.extra_sleep = max(0, round(self.extra_sleep - 0.1, 3))
                self.cache[isrc] = result
                return result
            except (ValueError, requests.RequestException) as error:
                logger.warning(
                    "Spotify lookup for %s failed (attempt %d/%d): %s",
                    isrc, attempt, MAX_ATTEMPTS, self.safe(str(error)),
                )
                if isinstance(error, SpotifyHTTPError):
                    if error.status == 401:
                        self.token = ""
                    elif error.status == 429:
                        self.retry_until = time.monotonic() + error.retry_after
                    elif error.status == 503:
                        self.extra_sleep = round(self.extra_sleep + 0.5, 3)
                        logger.info("Spotify temporary extra sleep increased to %.3f second(s)", self.extra_sleep)
                    elif 400 <= error.status < 500:
                        break
        self.failed.add(isrc)
        raise ValueError(f"Giving up on Spotify ISRC {isrc}; leaving its fields unchanged")


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


def run(
    specific_file: Path | None = None,
    specific_dir: Path | None = None,
    count: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    sleep: float = MIN_SLEEP,
    offline: bool = False,
    overwrite: bool = False,
) -> bool:
    if not math.isfinite(sleep) or sleep < MIN_SLEEP:
        raise ValueError(f"sleep must be a finite number of seconds >= {MIN_SLEEP:g}")
    if count is not None and count < 0:
        raise ValueError("count must be non-negative")
    if offline and not dry_run:
        raise ValueError("offline mode requires dry run")
    client_id = os.environ.get("SPOTIFY_API_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_API_CLIENT_SECRET", "").strip()
    if not offline and (not client_id or not client_secret):
        raise ValueError("SPOTIFY_API_CLIENT_ID and SPOTIFY_API_CLIENT_SECRET must be set")
    client = SpotifyClient(client_id, client_secret, sleep)
    succeeded = True
    processed = 0
    paths = track_files(specific_file, specific_dir)
    while count is None or processed < count:
        path = next(paths, None)
        if path is None:
            break
        try:
            original = path.read_text(encoding="utf-8")
            yaml = cast(_Yaml, YAML())
            document = yaml.load(original)
            if not is_mapping(document) or not is_mapping(document.get("trackData")):
                if specific_file is not None:
                    raise ValueError("Expected a track YAML document with a trackData mapping")
                logger.debug("[%s] Not a track YAML mapping; skipping", path)
                continue
            identifiers = document.get("trackIdData")
            if is_mapping(identifiers):
                # Detach aliases so Spotify writes cannot modify unrelated YAML mappings.
                document["trackIdData"] = deepcopy(identifiers)
            entries = identifier_entries(document, path)
            if not force:
                pending = [(entry, isrcs) for entry, isrcs in entries if not has_spotify_data(entry)]
                if len(pending) < len(entries):
                    logger.info("[%s] Skipping %d populated Spotify entries (use --force)", path, len(entries) - len(pending))
                entries = pending
            if not entries:
                logger.info("[%s] No eligible Spotify entries; skipping", path)
                continue
            processed += 1
            if offline:
                for isrc in dict.fromkeys(isrc for _, isrcs in entries for isrc, _ in isrcs):
                    url = search_url(isrc)
                    logger.info("[%s] Offline: Spotify request not sent: GET %s", path, url)
                    print(url, flush=True)
                continue
            changed = False
            lookup_succeeded = False
            for entry, isrcs in entries:
                for isrc, source_isrc in isrcs:
                    try:
                        matches = client.lookup(isrc)
                    except ValueError as error:
                        logger.error("[%s] %s", path, client.safe(str(error)))
                        succeeded = False
                        continue
                    if not matches:
                        logger.info("[%s] No Spotify match for %s; fields unchanged", path, isrc)
                        continue
                    if len(matches) > 1:
                        logger.info("[%s] %d Spotify matches for %s; using the first", path, len(matches), isrc)
                    match = matches[0]
                    spotify_entries = entry.get("streamingData", [])
                    existing = [
                        item for item in spotify_entries
                        if is_mapping(item) and item.get("streamingService") == "spotify"
                    ] if is_list(spotify_entries) else []
                    if not force and any(
                        item.get(key) and item[key] != value
                        for item in existing
                        for key, value in (
                            ("trackId", match["id"]),
                            ("trackUrl", match["external_urls"]["spotify"]),
                        )
                    ):
                        logger.warning("[%s] Spotify result conflicts with existing fields; use --force to replace", path)
                        continue
                    lookup_succeeded = True
                    changed = store_spotify_data(
                        document,
                        entry,
                        source_isrc,
                        match["id"],
                        match["external_urls"]["spotify"],
                        match["api_data"],
                    ) or changed
                    break
            if not changed and not (overwrite and lookup_succeeded):
                logger.info("[%s] Spotify fields unchanged; no YAML write", path)
                continue
            if path.read_text(encoding="utf-8") != original:
                raise ValueError("Track YAML changed during lookup; refusing to overwrite")
            stream = StringIO()
            yaml.dump(document, stream)
            proposed = stream.getvalue()
            if client.safe(proposed) != proposed:
                raise ValueError("Refusing to save YAML containing Spotify credentials or tokens")
            if dry_run:
                proposal_dir = Path(tempfile.mkdtemp(prefix="spotify-proposal-", dir="/tmp"))
                proposal_path = proposal_dir / path.name
                try:
                    with proposal_path.open("x", encoding="utf-8") as output:
                        output.write(proposed)
                except OSError:
                    proposal_path.unlink(missing_ok=True)
                    proposal_dir.rmdir()
                    raise
                logger.info(
                    "[%s] Dry run: source and backups unchanged; proposal retained at %s; remove when finished",
                    path, proposal_path,
                )
                command = shlex.join(["meld", str(path.resolve()), str(proposal_path)])
                logger.info("[%s] Diff command: %s", path, command)
            else:
                def write(output: TextWriter) -> None:
                    output.write(proposed)

                backup_path = path.with_name(path.name + ".bak")
                save_yaml_atomically(path, write, backup_path)
                logger.info("[%s] Saved Spotify fields; original backed up to %s", path, backup_path)
                command = shlex.join(["meld", str(backup_path.resolve()), str(path.resolve())])
            print(client.safe(command), flush=True)
        except YAML_ERRORS as error:
            logger.error("[%s] Spotify lookup failed: %s", path, client.safe(str(error)))
            succeeded = False
        except KeyboardInterrupt:
            logger.warning("[%s] Interrupted; pending updates are not saved automatically", path)
            raise
    logger.info("Processed %d track(s)", processed)
    return succeeded


def sleep_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number: {value!r}") from None
    if not math.isfinite(seconds) or seconds < MIN_SLEEP:
        raise argparse.ArgumentTypeError(f"must be a finite number of seconds >= {MIN_SLEEP:g}")
    return seconds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--file", type=Path, help="Process one track YAML file.")
    scope.add_argument("-d", "--dir", type=Path, help="Recursively scan this directory.")
    parser.add_argument("-c", "--count", type=int, help="Maximum eligible tracks (non-negative; default: unlimited).")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Write /tmp proposals and meld commands; do not modify sources.")
    parser.add_argument("--force", action="store_true", help="Re-query and replace existing Spotify fields.")
    parser.add_argument("--overwrite", action="store_true", help="Rewrite YAML even if a successful match is unchanged.")
    parser.add_argument("--sleep", type=sleep_seconds, default=MIN_SLEEP, help=f"Minimum request interval (default: {MIN_SLEEP:g}s).")
    parser.add_argument("--offline", action="store_true", help="With --dry-run, print search URLs without API requests.")
    args = parser.parse_args()
    if args.count is not None and args.count < 0:
        parser.error("--count must be non-negative")
    if args.offline and not args.dry_run:
        parser.error("--offline requires --dry-run")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return 0 if run(args.file, args.dir, args.count, args.dry_run, args.force, args.sleep, args.offline, args.overwrite) else 1
    except YAML_ERRORS as error:
        logger.error("%s", error)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
