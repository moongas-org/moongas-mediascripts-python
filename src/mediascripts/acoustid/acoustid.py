"""Look up track YAML media-file fingerprints and save full AcoustID responses."""

import argparse
import json
import logging
import math
import os
import shlex
import tempfile
import time
from collections.abc import Iterator, Mapping
from io import StringIO
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urlencode
from urllib.request import Request, urlopen

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from mediascripts.acoustid.recording_yaml import is_list, is_mapping, load_track, normalize_isrcs

from mediascripts.track.track_yaml.track_yaml_common import (
    TextWriter,
    iter_collection_files,
    save_yaml_atomically,
)

logger = logging.getLogger(__name__)
LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
MIN_SLEEP = 1 / 3 + 0.01
YAML_ERRORS = (OSError, UnicodeError, YAMLError, ValueError, TypeError)
RESERVED_NAMES = {"artist", "artists", "album", "files"}


class _Yaml(Protocol):
    def load(self, stream: str) -> object: ...
    def dump(self, data: Mapping[str, object], stream: TextWriter) -> None: ...


def redact(text: str, api_key: str) -> str:
    if api_key:
        variants = {
            api_key,
            quote(api_key, safe=""),
            quote_plus(api_key),
            json.dumps(api_key)[1:-1],
            repr(api_key)[1:-1],
        }
        for secret in sorted(variants, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
    return text


def log(level: int, api_key: str, message: str, *args: object) -> None:
    logger.log(level, "%s", redact(message % args if args else message, api_key))


def log_response_body(body: bytes, api_key: str) -> None:
    try:
        result: object = json.loads(body)
    except (ValueError, UnicodeError):
        log(
            logging.WARNING,
            api_key,
            "AcoustID response is not valid JSON; raw response body:\n%s",
            body.decode("utf-8", errors="replace"),
        )
    else:
        log(
            logging.INFO,
            api_key,
            "AcoustID raw response JSON:\n%s",
            json.dumps(result, indent=2),
        )


def build_request(api_key: str, duration: int, fingerprint: str) -> Request:
    params: list[tuple[str, str | int]] = [
        ("client", api_key),
        ("duration", duration),
        ("fingerprint", fingerprint),
        ("meta", "recordingids"),
        ("meta", "releases"),
        ("meta", "releasegroups"),
        ("meta", "isrcs"),
        ("format", "json"),
    ]
    return Request(
        f"{LOOKUP_URL}?{urlencode(params)}",
        headers={"User-Agent": "moongas-mediascripts/1.2.1"},
    )


def lookup(api_key: str, duration: int, fingerprint: str) -> dict[str, object]:
    request = build_request(api_key, duration, fingerprint)
    log(
        logging.INFO,
        api_key,
        "AcoustID request: %s %s headers=%s timeout=30s",
        request.get_method(),
        request.full_url,
        json.dumps(dict(request.header_items())),
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
            log(logging.INFO, api_key, "AcoustID response: HTTP %s", response.status)
            log_response_body(body, api_key)
    except HTTPError as error:
        with error:
            body = error.read()
        log(logging.INFO, api_key, "AcoustID response: HTTP %d", error.code)
        log_response_body(body, api_key)
        raise ValueError(f"AcoustID returned HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError) as error:
        raise ValueError(
            f"AcoustID request failed: {redact(str(error), api_key)}"
        ) from None
    result: object = json.loads(body)
    if not is_mapping(result):
        raise ValueError("AcoustID response must be a JSON object")
    if result.get("status") != "ok":
        raise ValueError("AcoustID reported an API error; see logged result")
    validate_response(result)
    return result


def validate_response(result: Mapping[str, object]) -> None:
    """Validate a lookup response without changing it."""
    matches = result.get("results")
    if not is_list(matches):
        raise ValueError("AcoustID results must be a list")
    for match in matches:
        if (
            not is_mapping(match)
            or not isinstance(match.get("id"), str)
            or not match["id"]
        ):
            raise ValueError("AcoustID match must have a non-empty string ID")
        recordings: object = match.get("recordings", [])
        if not is_list(recordings):
            raise ValueError("AcoustID recordings must be a list")
        for recording in recordings:
            if (
                not is_mapping(recording)
                or not isinstance(recording.get("id"), str)
                or not recording["id"]
            ):
                raise ValueError("AcoustID recording must have a non-empty string ID")
            isrcs = recording.get("isrcs")
            if isrcs is None:
                continue
            if not is_list(isrcs) or not all(isinstance(isrc, str) and isrc for isrc in isrcs):
                raise ValueError(
                    "AcoustID recording ISRCs must be a list of non-empty strings"
                )


def fingerprint_entries(
    document: dict[str, object], path: Path, api_key: str
) -> list[tuple[dict[str, object], int, str]]:
    media_files: object = document.get("mediaFileData", [])
    if media_files is None:
        media_files = []
    if not is_list(media_files):
        raise ValueError("mediaFileData must be a list")
    entries: list[tuple[dict[str, object], int, str]] = []
    for index, entry in enumerate(media_files):
        if not is_mapping(entry):
            raise ValueError("mediaFileData entries must be mappings")
        data = entry.get("fpcalcData")
        if data is None:
            continue
        if not is_mapping(data):
            raise ValueError("fpcalcData must be a mapping")
        fingerprint = data.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            log(
                logging.WARNING,
                api_key,
                "[%s] mediaFileData[%d]: missing fpcalc fingerprint; skipping",
                path,
                index,
            )
            continue
        duration = data.get("duration")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or int(duration) <= 0
        ):
            log(
                logging.WARNING,
                api_key,
                "[%s] mediaFileData[%d]: invalid fpcalc duration; skipping",
                path,
                index,
            )
            continue
        entries.append((entry, int(duration), fingerprint.strip()))
    if not entries:
        log(
            logging.WARNING,
            api_key,
            "[%s] No usable fpcalc fingerprint and duration; skipping lookup",
            path,
        )
    return entries


def track_files(
    specific_file: Path | None, specific_dir: Path | None
) -> Iterator[Path]:
    if specific_file is not None:
        if (
            not specific_file.is_file()
            or specific_file.suffix.lower() not in {".yml", ".yaml"}
            or specific_file.stem.lower() in RESERVED_NAMES
        ):
            raise ValueError("--file must name an existing track .yml or .yaml file")
        yield specific_file
        return
    if specific_dir is None:
        root = os.environ.get("MOONGAS_COLLECTION_ROOTDIR")
        if not root:
            raise ValueError(
                "MOONGAS_COLLECTION_ROOTDIR must be set, or use --file/--dir"
            )
        specific_dir = Path(root) / "data"
    if not specific_dir.is_dir():
        raise ValueError(f"Input directory does not exist: {specific_dir}")
    for path in iter_collection_files(specific_dir, album_directories_only=False):
        if (
            path.suffix.lower() in {".yml", ".yaml"}
            and path.stem.lower() not in RESERVED_NAMES
        ):
            yield path


def run(
    api_key: str,
    specific_file: Path | None = None,
    specific_dir: Path | None = None,
    count: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    sleep: float = MIN_SLEEP,
    offline: bool = False,
    overwrite: bool = False,
) -> bool:
    if not api_key.strip():
        raise ValueError("ACOUSTID_API_KEY must be set")
    if offline and not dry_run:
        raise ValueError("offline mode requires dry run")
    if not math.isfinite(sleep) or sleep < MIN_SLEEP:
        raise ValueError(f"sleep must be a finite number of seconds >= {MIN_SLEEP:g}")
    if count is not None and count < 0:
        raise ValueError("count must be non-negative")
    succeeded = True
    processed = 0
    last_request: float | None = None
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
                    raise ValueError(
                        "Expected a track YAML document with a trackData mapping"
                    )
                log(
                    logging.DEBUG,
                    api_key,
                    "[%s] Not a track YAML mapping; skipping",
                    path,
                )
                continue
            repaired = normalize_isrcs(document) if not offline else False
            entries = fingerprint_entries(document, path, api_key)
            if not entries and not repaired and not recovered:
                continue
            if not force:
                pending = [item for item in entries if not item[0].get("acoustidData")]
                if len(pending) < len(entries):
                    log(
                        logging.INFO,
                        api_key,
                        "[%s] %d fingerprint(s) already have acoustidData; skipping (use --force to look up again)",
                        path,
                        len(entries) - len(pending),
                    )
                entries = pending
                if not entries and not repaired and not recovered:
                    continue
            if entries:
                processed += 1
            if offline:
                for _, duration, fingerprint in entries:
                    url = build_request(api_key, duration, fingerprint).full_url
                    log(
                        logging.INFO,
                        api_key,
                        "[%s] Offline: AcoustID request not sent: GET %s",
                        path,
                        url,
                    )
                    print(redact(url, api_key), flush=True)
                continue
            changed = repaired or recovered
            for entry, duration, fingerprint in entries:
                if last_request is not None:
                    delay = max(0, sleep - (time.monotonic() - last_request))
                    if delay > 0:
                        log(
                            logging.INFO,
                            api_key,
                            "Sleeping %.3f second(s) before next AcoustID request",
                            delay,
                        )
                        time.sleep(delay)
                last_request = time.monotonic()
                result = lookup(api_key, duration, fingerprint)
                if entry.get("acoustidData") != result:
                    entry["acoustidData"] = result
                    changed = True
            if changed or (overwrite and entries):
                normalize_isrcs(document)
                if path.read_text(encoding="utf-8") != original:
                    raise ValueError(
                        "Track YAML changed during lookup; refusing to overwrite"
                    )
                stream = StringIO()
                yaml.dump(document, stream)
                proposed = stream.getvalue()
                if redact(proposed, api_key) != proposed:
                    raise ValueError("Refusing to save YAML containing the API key")

                if dry_run:
                    proposal_dir = Path(
                        tempfile.mkdtemp(prefix="acoustid-proposal-", dir="/tmp")
                    )
                    proposal_path = proposal_dir / path.name
                    try:
                        with proposal_path.open("x", encoding="utf-8") as output:
                            output.write(proposed)
                    except OSError:
                        proposal_path.unlink(missing_ok=True)
                        proposal_dir.rmdir()
                        raise
                    log(
                        logging.INFO,
                        api_key,
                        "[%s] Dry run: source and backups unchanged; proposal retained at %s; remove when finished",
                        path,
                        proposal_path,
                    )
                    command = shlex.join(
                        ["meld", str(path.resolve()), str(proposal_path)]
                    )
                    print(redact(command, api_key), flush=True)
                    continue

                def write_yaml(output: TextWriter) -> None:
                    output.write(proposed)

                backup_path = path.with_name(path.name + ".bak")
                save_yaml_atomically(path, write_yaml, backup_path)
                log(
                    logging.INFO,
                    api_key,
                    "[%s] Saved AcoustID response; original backed up to %s.bak",
                    path,
                    path,
                )
                command = shlex.join(
                    ["meld", str(backup_path.resolve()), str(path.resolve())]
                )
                print(redact(command, api_key), flush=True)
            else:
                log(logging.INFO, api_key, "[%s] AcoustID response unchanged", path)
        except YAML_ERRORS as error:
            log(logging.ERROR, api_key, "[%s] Lookup failed: %s", path, error)
            succeeded = False
    log(logging.INFO, api_key, "Processed %d track(s)", processed)
    return succeeded


def sleep_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number: {value!r}") from None
    if not math.isfinite(seconds) or seconds < MIN_SLEEP:
        raise argparse.ArgumentTypeError(
            f"must be a finite number of seconds >= {MIN_SLEEP:g}"
        )
    return seconds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--file", type=Path, help="Process one track YAML file.")
    scope.add_argument(
        "-d", "--dir", type=Path, help="Recursively scan this directory."
    )
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
        help="Also look up fingerprints that already have non-empty acoustidData.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Rewrite YAML after a successful lookup even if the response is unchanged.",
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
        help="With --dry-run, print each request URL (API key redacted) instead of sending it.",
    )
    args = parser.parse_args()
    if args.count is not None and args.count < 0:
        parser.error("--count must be non-negative")
    if args.offline and not args.dry_run:
        parser.error("--offline requires --dry-run")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    api_key = os.environ.get("ACOUSTID_API_KEY", "")
    try:
        return (
            0
            if run(
                api_key,
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
        log(logging.ERROR, api_key, "%s", error)
        return 1
    except KeyboardInterrupt:
        log(logging.INFO, api_key, "Interrupted by user; exiting")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
