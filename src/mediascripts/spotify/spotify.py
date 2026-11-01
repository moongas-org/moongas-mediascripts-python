"""Add Spotify streaming data to track YAML using streaming and recording ISRCs."""

import argparse
import logging
import math
import os
import random
import shlex
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from io import StringIO
from pathlib import Path
from typing import Protocol, cast

from ruamel.yaml import YAML

from mediascripts.acoustid.acoustid import YAML_ERRORS, track_files
from mediascripts.acoustid.recording_yaml import is_list, is_mapping
from mediascripts.spotify.client import MIN_SLEEP, SpotifyClient, search_url
from mediascripts.spotify.track_yaml import has_spotify_data, identifier_entries, store_spotify_data
from mediascripts.track.track_yaml.track_yaml_common import TextWriter, save_yaml_atomically

logger = logging.getLogger(__name__)


class _Yaml(Protocol):
    def load(self, stream: str) -> object: ...
    def dump(self, data: Mapping[str, object], stream: TextWriter) -> None: ...


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
    track_paths = list(track_files(specific_file, specific_dir))
    random.shuffle(track_paths)
    paths = iter(track_paths)
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
                for isrc_index, (isrc, source_isrc) in enumerate(isrcs):
                    logger.info("[%s] Looking up Spotify track for ISRC %s", path, isrc)
                    try:
                        matches = client.lookup(isrc)
                    except ValueError as error:
                        logger.error("[%s] %s", path, client.safe(str(error)))
                        succeeded = False
                        continue
                    if not matches:
                        logger.info(
                            "[%s] No Spotify match for %s; %d ISRC(s) remaining to try",
                            path, isrc, len(isrcs) - isrc_index - 1,
                        )
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
