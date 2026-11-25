"""Rate-limited MusicBrainz catalogue access shared by collection scripts."""

import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mediascan.types.track.track_yaml_file_validator import is_mapping

from mediascripts.acoustid.musicbrainz import MIN_SLEEP, USER_AGENT
from mediascripts.common.http import retry_after_seconds

logger = logging.getLogger(__name__)
API_URL = "https://musicbrainz.org/ws/2"
MAX_ATTEMPTS = 4
MAX_RETRY_DELAY = 120.0
RETRY_STATUSES = {429, 500, 502, 503, 504}


class MusicBrainz:
    """Pace all catalogue requests, including searches and release lookups."""

    def __init__(self) -> None:
        self.last_request: float | None = None

    def get(self, endpoint: str, **params: str) -> dict[str, object]:
        request = Request(
            f"{API_URL}/{endpoint}?{urlencode({**params, 'fmt': 'json'})}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.last_request is not None:
                delay = max(0.0, MIN_SLEEP - (time.monotonic() - self.last_request))
                logger.debug("MusicBrainz rate-limit wait: %.2fs", delay)
                time.sleep(delay)
            logger.info("MusicBrainz lookup: %s (attempt %d/%d)", request.full_url, attempt, MAX_ATTEMPTS)
            self.last_request = time.monotonic()
            started = time.perf_counter()
            retry_delay = min(MAX_RETRY_DELAY, 2.0 ** attempt)
            try:
                with urlopen(request, timeout=30) as response:
                    logger.info("MusicBrainz response: HTTP %s", getattr(response, "status", "unknown"))
                    result: object = json.load(response)
            except HTTPError as error:
                retryable = error.code in RETRY_STATUSES
                retry_after = error.headers.get("Retry-After")
                logger.warning(
                    "MusicBrainz HTTP %d after %.2fs (attempt %d/%d): %s",
                    error.code, time.perf_counter() - started, attempt, MAX_ATTEMPTS, request.full_url,
                )
                error.close()
                if not retryable or attempt == MAX_ATTEMPTS:
                    raise
                if retry_after:
                    retry_delay = retry_after_delay(retry_after, retry_delay)
            except (URLError, TimeoutError, ConnectionError) as error:
                logger.warning(
                    "MusicBrainz connection failure after %.2fs (attempt %d/%d): %s",
                    time.perf_counter() - started, attempt, MAX_ATTEMPTS, error,
                )
                if attempt == MAX_ATTEMPTS:
                    raise
            else:
                logger.info("MusicBrainz lookup completed in %.2fs", time.perf_counter() - started)
                logger.debug("MusicBrainz response content: %s", json.dumps(result, ensure_ascii=False))
                if not is_mapping(result):
                    raise ValueError("MusicBrainz response must be a mapping")
                return result
            logger.warning("Retrying MusicBrainz in %.2fs", retry_delay)
            time.sleep(retry_delay)
        raise RuntimeError("MusicBrainz retry loop exhausted unexpectedly")


def retry_after_delay(value: str, fallback: float) -> float:
    seconds = retry_after_seconds(value)
    if seconds is not None:
        return min(MAX_RETRY_DELAY, seconds)
    logger.warning("Invalid MusicBrainz Retry-After header %r; using %.2fs backoff", value, fallback)
    return fallback


def lucene_phrase(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
