"""Rate-limited MusicBrainz catalogue access shared by collection scripts."""

import json
import logging
import math
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


def validate_retry_options(max_attempts: int, retry_delay: float, max_retry_delay: float) -> None:
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    if not math.isfinite(retry_delay) or retry_delay <= 0:
        raise ValueError("retry_delay must be a finite number of seconds > 0")
    if not math.isfinite(max_retry_delay) or max_retry_delay < retry_delay:
        raise ValueError("max_retry_delay must be finite and >= retry_delay")


class MusicBrainz:
    """Pace all catalogue requests, including searches and release lookups."""

    def __init__(
        self, sleep: float = MIN_SLEEP, *, max_attempts: int = MAX_ATTEMPTS,
        retry_delay: float = 2.0, max_retry_delay: float = MAX_RETRY_DELAY,
    ) -> None:
        if not math.isfinite(sleep) or sleep < MIN_SLEEP:
            raise ValueError(f"sleep must be a finite number of seconds >= {MIN_SLEEP:g}")
        validate_retry_options(max_attempts, retry_delay, max_retry_delay)
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.max_retry_delay = max_retry_delay
        self.last_request: float | None = None

    def get(self, endpoint: str, **params: str) -> dict[str, object]:
        request = Request(
            f"{API_URL}/{endpoint}?{urlencode({**params, 'fmt': 'json'})}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        retry_delay = self.retry_delay
        for attempt in range(1, self.max_attempts + 1):
            if self.last_request is not None:
                delay = max(0.0, self.sleep - (time.monotonic() - self.last_request))
                logger.info(
                    "MusicBrainz rate-limit wait: %.2fs (minimum request-start interval: %.2fs; attempt %d/%d)",
                    delay, self.sleep, attempt, self.max_attempts,
                )
                time.sleep(delay)
            logger.info("MusicBrainz lookup: %s (attempt %d/%d)", request.full_url, attempt, self.max_attempts)
            self.last_request = time.monotonic()
            started = time.perf_counter()
            wait = retry_delay
            try:
                with urlopen(request, timeout=30) as response:
                    logger.info("MusicBrainz response: HTTP %s", getattr(response, "status", "unknown"))
                    result: object = json.load(response)
            except HTTPError as error:
                retryable = error.code in RETRY_STATUSES
                retry_after = error.headers.get("Retry-After")
                logger.warning(
                    "MusicBrainz HTTP %d after %.2fs (attempt %d/%d): %s",
                    error.code, time.perf_counter() - started, attempt, self.max_attempts, request.full_url,
                )
                error.close()
                if not retryable or attempt == self.max_attempts:
                    logger.error(
                        "MusicBrainz request failed: %s (%s)",
                        request.full_url,
                        "retry limit exhausted" if retryable else "non-retryable HTTP status",
                    )
                    raise
                if retry_after:
                    wait = retry_after_delay(retry_after, retry_delay)
                    logger.info(
                        "MusicBrainz Retry-After header: %r; exponential backoff: %.2fs; effective backoff: %.2fs",
                        retry_after, retry_delay, wait,
                    )
            except (URLError, TimeoutError, ConnectionError) as error:
                logger.warning(
                    "MusicBrainz connection failure after %.2fs (attempt %d/%d): %s",
                    time.perf_counter() - started, attempt, self.max_attempts, error,
                )
                if attempt == self.max_attempts:
                    logger.error("MusicBrainz request failed: %s (retry limit exhausted)", request.full_url)
                    raise
            else:
                logger.info("MusicBrainz lookup completed in %.2fs", time.perf_counter() - started)
                logger.debug("MusicBrainz response content: %s", json.dumps(result, ensure_ascii=False))
                if not is_mapping(result):
                    raise ValueError("MusicBrainz response must be a mapping")
                return result
            logger.warning(
                "Retrying MusicBrainz in %.2fs backoff; the %.2fs minimum request-start interval "
                "is enforced separately before the next attempt",
                wait, self.sleep,
            )
            time.sleep(wait)
            retry_delay = min(self.max_retry_delay, retry_delay * 2)
        raise RuntimeError("MusicBrainz retry loop exhausted unexpectedly")


def retry_after_delay(value: str, fallback: float) -> float:
    seconds = retry_after_seconds(value)
    if seconds is not None:
        return max(fallback, seconds)
    logger.warning("Invalid MusicBrainz Retry-After header %r; using %.2fs backoff", value, fallback)
    return fallback


def lucene_phrase(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
