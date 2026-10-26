"""Fill AcoustID recordings in track YAML acoustidData with MusicBrainz recording details."""

import argparse
import json
import logging
import math
import shlex
import tempfile
import time
import uuid
from io import StringIO
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ruamel.yaml import YAML

from mediascripts.acoustid.acoustid import YAML_ERRORS, is_list, is_mapping, track_files
from mediascripts.acoustid.recording_yaml import load_track, normalize_isrcs
from mediascripts.track.track_yaml.track_yaml_common import (
    TextWriter,
    save_yaml_atomically,
)

logger = logging.getLogger(__name__)
RECORDING_URL = "https://musicbrainz.org/ws/2/recording/"
# MusicBrainz allows an average of one request per second per client.
MIN_SLEEP = 1 + 0.01
# MusicBrainz response keys mapped to TrackAcoustIDRecording YAML keys (camelCase, as mediascan writes them).
FIELDS = {
    "title": "title",
    "length": "length",
    "video": "video",
    "disambiguation": "disambiguation",
    "first-release-date": "firstReleaseDate",
}
FIELD_CHECKS: dict[str, Callable[[object], bool]] = {
    "title": lambda value: isinstance(value, str),
    "length": lambda value: isinstance(value, int) and not isinstance(value, bool) and value >= 0,
    "video": lambda value: isinstance(value, bool),
    "disambiguation": lambda value: isinstance(value, str),
    "firstReleaseDate": lambda value: isinstance(value, str),
}
MAX_ATTEMPTS = 5
EXTRA_SLEEP_INCREASE = 0.5
EXTRA_SLEEP_DECREASE = 0.1
USER_AGENT = "moongas-mediascripts/1.2.1 ( https://github.com/moongas-org/moongas-mediascripts-python )"


class _Yaml(Protocol):
    def load(self, stream: str) -> object: ...
    def dump(self, data: Mapping[str, object], stream: TextWriter) -> None: ...


class RecordingNotFound(Exception):
    pass


class ExtraSleep:
    """Temporary extra sleep added to --sleep: grows on HTTP 503 and shrinks on later 2xx responses."""

    def __init__(self) -> None:
        self.seconds = 0.0

    def record(self, status: int) -> None:
        previous = self.seconds
        if status == 503:
            self.seconds = round(self.seconds + EXTRA_SLEEP_INCREASE, 3)
        elif 200 <= status < 300 and self.seconds > 0:
            self.seconds = max(0.0, round(self.seconds - EXTRA_SLEEP_DECREASE, 3))
        else:
            return
        logger.info(
            "MusicBrainz HTTP %d: temporary extra sleep %s from %.3f to %.3f second(s)",
            status,
            "increased" if self.seconds > previous else "decreased",
            previous,
            self.seconds,
        )


def build_request(recording_id: str) -> Request:
    params = [("inc", "isrcs"), ("fmt", "json")]
    return Request(
        f"{RECORDING_URL}{recording_id}?{urlencode(params)}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


def log_response_body(body: bytes) -> None:
    try:
        result: object = json.loads(body)
    except (ValueError, UnicodeError):
        logger.warning(
            "MusicBrainz response is not valid JSON; raw response body:\n%s",
            body.decode("utf-8", errors="replace"),
        )
    else:
        logger.info("MusicBrainz raw response JSON:\n%s", json.dumps(result, indent=2))


def lookup(recording_id: str, extra_sleep: ExtraSleep | None = None) -> dict[str, object]:
    """Return the recording's details keyed by YAML field; raise RecordingNotFound on HTTP 404."""
    request = build_request(recording_id)
    logger.info(
        "MusicBrainz request: %s %s headers=%s timeout=30s",
        request.get_method(),
        request.full_url,
        json.dumps(dict(request.header_items())),
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
            logger.info("MusicBrainz response: HTTP %s", response.status)
            log_response_body(body)
        if extra_sleep is not None:
            extra_sleep.record(response.status)
    except HTTPError as error:
        with error:
            body = error.read()
        logger.info("MusicBrainz response: HTTP %d", error.code)
        log_response_body(body)
        if extra_sleep is not None:
            extra_sleep.record(error.code)
        if error.code == 404:
            raise RecordingNotFound(recording_id) from None
        raise ValueError(f"MusicBrainz returned HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError) as error:
        raise ValueError(f"MusicBrainz request failed: {error}") from None
    result: object = json.loads(body)
    return validate_response(result, recording_id)


def validate_response(result: object, recording_id: str) -> dict[str, object]:
    if not is_mapping(result):
        raise ValueError("MusicBrainz response must be a JSON object")
    returned_id = result.get("id")
    if not isinstance(returned_id, str) or not returned_id:
        raise ValueError("MusicBrainz recording must have a non-empty string ID")
    if returned_id != recording_id:
        logger.warning(
            "MusicBrainz recording %s resolved to %s (merged recording?)",
            recording_id,
            returned_id,
        )
    details: dict[str, object] = {}
    for response_key, yaml_key in FIELDS.items():
        value = result.get(response_key)
        if value is not None and not FIELD_CHECKS[yaml_key](value):
            raise ValueError(f"MusicBrainz recording {response_key} has an invalid value: {value!r}")
        details[yaml_key] = value
    isrcs = result.get("isrcs", [])
    if not is_list(isrcs) or not all(isinstance(isrc, str) and isrc for isrc in isrcs):
        raise ValueError("MusicBrainz recording ISRCs must be a list of non-empty strings")
    details["isrcs"] = list(isrcs)
    return details


def is_recording_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def acoustid_recordings(document: dict[str, object], path: Path) -> list[dict[str, object]]:
    identifiers: object = document.get("trackIdData", [])
    if identifiers is None:
        identifiers = []
    if not is_list(identifiers):
        raise ValueError("trackIdData must be a list")
    recordings: list[dict[str, object]] = []
    for index, entry in enumerate(identifiers):
        if not is_mapping(entry):
            raise ValueError("trackIdData entries must be mappings")
        data = entry.get("acoustidData")
        if not data:
            continue
        if not is_mapping(data):
            raise ValueError("acoustidData must be a mapping")
        results = data.get("results", [])
        if not is_list(results):
            raise ValueError("acoustidData results must be a list")
        for result in results:
            if not is_mapping(result):
                raise ValueError("acoustidData results must be mappings")
            matches: object = result.get("recordings", [])
            if not is_list(matches):
                raise ValueError("acoustidData recordings must be a list")
            for recording in matches:
                if not is_mapping(recording):
                    raise ValueError("acoustidData recordings must be mappings")
                if not is_recording_id(recording.get("id")):
                    logger.warning(
                        "[%s] trackIdData[%d]: invalid MusicBrainz recording ID %r; skipping",
                        path,
                        index,
                        recording.get("id"),
                    )
                    continue
                recordings.append(recording)
    return recordings


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
    succeeded = True
    processed = 0
    last_request: float | None = None
    extra_sleep = ExtraSleep()
    # Responses are cached per run so recordings shared by several matches or tracks are requested once.
    cache: dict[str, dict[str, object] | None] = {}
    failed: set[str] = set()
    paths = track_files(specific_file, specific_dir)
    while count is None or processed < count:
        path = next(paths, None)
        if path is None:
            break
        try:
            original, document, recovered = load_track(path, dry_run=dry_run or offline)
            yaml = cast(_Yaml, YAML())
            if not is_mapping(document) or not is_mapping(document.get("trackData")):
                if specific_file is not None:
                    raise ValueError("Expected a track YAML document with a trackData mapping")
                logger.debug("[%s] Not a track YAML mapping; skipping", path)
                continue
            repaired = normalize_isrcs(document) if not offline else False
            recordings = acoustid_recordings(document, path)
            if not recordings:
                logger.warning(
                    "[%s] No AcoustID recording IDs in acoustidData; skipping (run acoustid first)",
                    path,
                )
                if not repaired and not recovered:
                    continue
            if not force:
                pending = [recording for recording in recordings if not recording.get("isrcs")]
                if len(pending) < len(recordings):
                    logger.info(
                        "[%s] %d recording(s) already have ISRCs; skipping (use --force to look up again)",
                        path,
                        len(recordings) - len(pending),
                    )
                recordings = pending
                if not recordings and not repaired and not recovered:
                    continue
            if recordings:
                processed += 1
            if offline:
                for recording_id in dict.fromkeys(cast(str, recording["id"]) for recording in recordings):
                    url = build_request(recording_id).full_url
                    logger.info("[%s] Offline: MusicBrainz request not sent: GET %s", path, url)
                    print(url, flush=True)
                continue
            changed = repaired or recovered
            lookup_succeeded = False
            track_failed = False
            logger.info(
                "[%s] Processing %d recording entry/entries (%d distinct IDs); dry_run=%s, overwrite=%s. "
                "Changes stay in memory until all recording entries are processed; then YAML and meld output are produced.",
                path,
                len(recordings),
                len({cast(str, recording["id"]) for recording in recordings}),
                dry_run,
                overwrite,
            )
            for recording_index, recording in enumerate(recordings, start=1):
                recording_id = cast(str, recording["id"])
                logger.info(
                    "[%s] Recording %d/%d: %s",
                    path,
                    recording_index,
                    len(recordings),
                    recording_id,
                )
                if recording_id in failed:
                    logger.warning(
                        "[%s] MusicBrainz lookup for %s already failed %d times this run; leaving unchanged",
                        path,
                        recording_id,
                        MAX_ATTEMPTS,
                    )
                    track_failed = True
                    continue
                if recording_id in cache:
                    logger.info(
                        "[%s] Using MusicBrainz result already fetched for %s",
                        path,
                        recording_id,
                    )
                else:
                    for attempt in range(1, MAX_ATTEMPTS + 1):
                        if last_request is not None:
                            delay = max(
                                0,
                                sleep + extra_sleep.seconds - (time.monotonic() - last_request),
                            )
                            if delay > 0:
                                logger.info(
                                    "Sleeping %.3f second(s) before next MusicBrainz request (temporary extra sleep %.3f)",
                                    delay,
                                    extra_sleep.seconds,
                                )
                                time.sleep(delay)
                        last_request = time.monotonic()
                        logger.info(
                            "[%s] Looking up %s (attempt %d/%d)",
                            path,
                            recording_id,
                            attempt,
                            MAX_ATTEMPTS,
                        )
                        try:
                            cache[recording_id] = lookup(recording_id, extra_sleep)
                            break
                        except RecordingNotFound:
                            logger.warning(
                                "[%s] MusicBrainz recording %s not found; leaving unchanged",
                                path,
                                recording_id,
                            )
                            cache[recording_id] = None
                            break
                        except ValueError as error:
                            logger.warning(
                                "[%s] MusicBrainz lookup for %s failed (attempt %d of %d): %s",
                                path,
                                recording_id,
                                attempt,
                                MAX_ATTEMPTS,
                                error,
                            )
                    else:
                        logger.error(
                            "[%s] Giving up on MusicBrainz recording %s after %d failed attempts; leaving unchanged",
                            path,
                            recording_id,
                            MAX_ATTEMPTS,
                        )
                        failed.add(recording_id)
                        track_failed = True
                        continue
                details = cache[recording_id]
                if details is None:
                    logger.info("[%s] Recording %s: no result; no update staged", path, recording_id)
                    continue
                lookup_succeeded = True
                updated_fields: list[str] = []
                for key, value in details.items():
                    if key not in recording or recording[key] != value:
                        recording[key] = list(value) if is_list(value) else value
                        changed = True
                        updated_fields.append(key)
                logger.info(
                    "[%s] Recording %s: %s; not yet written to YAML",
                    path,
                    recording_id,
                    "staged changes to " + ", ".join(updated_fields) if updated_fields else "details unchanged",
                )
            if track_failed:
                succeeded = False
            logger.info(
                "[%s] Finished processing recording entries; changes_pending=%s, successful_result=%s, lookup_failures=%s",
                path,
                changed,
                lookup_succeeded,
                track_failed,
            )
            if changed or (overwrite and lookup_succeeded):
                logger.info(
                    "[%s] %s: %s",
                    path,
                    "Writing dry-run proposal" if dry_run else "Writing track YAML",
                    "pending changes" if changed else "--overwrite requested despite unchanged recording details",
                )
                normalize_isrcs(document)
                if path.read_text(encoding="utf-8") != original:
                    raise ValueError("Track YAML changed during lookup; refusing to overwrite")
                stream = StringIO()
                yaml.dump(document, stream)
                proposed = stream.getvalue()
                if dry_run:
                    proposal_dir = Path(tempfile.mkdtemp(prefix="musicbrainz-proposal-", dir="/tmp"))
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
                        path,
                        proposal_path,
                    )
                    command = shlex.join(["meld", str(path.resolve()), str(proposal_path)])
                    logger.info("[%s] Diff command: %s", path, command)
                    print(command, flush=True)
                    continue

                def write_yaml(output: TextWriter) -> None:
                    output.write(proposed)

                backup_path = path.with_name(path.name + ".bak")
                save_yaml_atomically(path, write_yaml, backup_path)
                logger.info(
                    "[%s] Saved MusicBrainz recording details; original backed up to %s",
                    path,
                    backup_path,
                )
                command = shlex.join(["meld", str(backup_path.resolve()), str(path.resolve())])
                logger.info("[%s] Diff command: %s", path, command)
                print(command, flush=True)
            else:
                logger.info(
                    "[%s] No YAML write and no meld command: changes_pending=%s, overwrite=%s, successful_result=%s",
                    path,
                    changed,
                    overwrite,
                    lookup_succeeded,
                )
        except KeyboardInterrupt:
            logger.warning(
                "[%s] Interrupted while processing this track. Recording updates are saved only after all entries "
                "finish; check for a preceding 'Saved MusicBrainz recording details' or 'proposal retained' "
                "message to confirm a completed write. No automatic save of pending updates on interruption.",
                path,
            )
            raise
        except YAML_ERRORS as error:
            logger.error("[%s] Lookup failed: %s", path, error)
            succeeded = False
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
    parser.add_argument(
        "-c",
        "--count",
        type=int,
        help="Maximum eligible tracks (non-negative; default: unlimited).",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Perform lookups and write proposals under /tmp with meld commands; leave source files unchanged.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Also look up recordings that already have ISRCs.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Rewrite YAML after a successful lookup even if the recording details are unchanged.",
    )
    parser.add_argument(
        "--sleep",
        type=sleep_seconds,
        default=MIN_SLEEP,
        help=f"Minimum seconds between API requests (default and minimum: {MIN_SLEEP:g}).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="With --dry-run, print each request URL instead of sending it.",
    )
    args = parser.parse_args()
    if args.count is not None and args.count < 0:
        parser.error("--count must be non-negative")
    if args.offline and not args.dry_run:
        parser.error("--offline requires --dry-run")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return (
            0
            if run(
                args.file,
                args.dir,
                args.count,
                args.dry_run,
                args.force,
                args.sleep,
                args.offline,
                args.overwrite,
            )
            else 1
        )
    except YAML_ERRORS as error:
        logger.error("%s", error)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
