"""Spotify HTTP requests, response validation, rate limiting, and lookup caching."""

import json
import logging
import math
import time
from typing import TypedDict
from urllib.parse import urlencode

import requests

from mediascripts.acoustid.acoustid import redact
from mediascripts.acoustid.recording_yaml import is_list, is_mapping

logger = logging.getLogger(__name__)
TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
MIN_SLEEP = 1.01
MAX_ATTEMPTS = 5
USER_AGENT = "moongas-mediascripts/1.2.7"


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
        logger.warning(
            "Invalid Spotify Retry-After header; using normal request spacing"
        )
        return 0
    if not math.isfinite(seconds) or seconds < 0:
        logger.warning(
            "Invalid Spotify Retry-After header; using normal request spacing"
        )
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
        logger.warning(
            "Spotify response is not valid JSON; raw body:\n%s", safe(response.text)
        )
        if response.status_code == 200:
            raise ValueError("Spotify response is not valid JSON") from None
        payload = None
    else:
        logger.info(
            "Spotify raw response JSON:\n%s", safe(json.dumps(payload, indent=2))
        )
    if response.status_code != 200:
        raise SpotifyHTTPError(response.status_code, retry_after_seconds(response))
    return payload


def get_access_token(client_id: str, client_secret: str) -> str:
    logger.info(
        "Spotify request: POST %s credentials=[REDACTED] timeout=30s", TOKEN_URL
    )
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        timeout=30,
    )
    # Never log a token response body, including malformed or error responses.
    logger.info(
        "Spotify authentication response: HTTP %d (body redacted)", response.status_code
    )
    if response.status_code != 200:
        raise SpotifyHTTPError(response.status_code, retry_after_seconds(response))
    try:
        payload: object = response.json()
    except ValueError:
        raise ValueError(
            "Spotify authentication response is not valid JSON (body redacted)"
        ) from None
    if not is_mapping(payload):
        raise ValueError("Spotify authentication response must be a JSON object")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError("Spotify authentication response has no access token")
    return access_token


def get_track_by_isrc(isrc: str, access_token: str) -> list[SpotifyTrack]:
    logger.info(
        "Spotify request: GET %s headers={Authorization: [REDACTED]} timeout=30s",
        search_url(isrc),
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
        name, track_id, external_urls = (
            item.get("name"),
            item.get("id"),
            item.get("external_urls"),
        )
        spotify_url = (
            external_urls.get("spotify") if is_mapping(external_urls) else None
        )
        if (
            not isinstance(name, str)
            or not isinstance(track_id, str)
            or not track_id.strip()
        ):
            raise ValueError(
                "Spotify track must have a string name and non-empty string ID"
            )
        if (
            not isinstance(spotify_url, str)
            or spotify_url != f"https://open.spotify.com/track/{track_id}"
        ):
            raise ValueError(
                "Spotify track must have a Spotify track URL matching its ID"
            )
        result.append(
            {
                "name": name,
                "id": track_id,
                "external_urls": {"spotify": spotify_url},
                "api_data": payload,
            }
        )
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
                    isrc,
                    attempt,
                    MAX_ATTEMPTS,
                    self.safe(str(error)),
                )
                if isinstance(error, SpotifyHTTPError):
                    if error.status == 401:
                        self.token = ""
                    elif error.status == 429:
                        self.retry_until = time.monotonic() + error.retry_after
                    elif error.status == 503:
                        self.extra_sleep = round(self.extra_sleep + 0.5, 3)
                        logger.info(
                            "Spotify temporary extra sleep increased to %.3f second(s)",
                            self.extra_sleep,
                        )
                    elif 400 <= error.status < 500:
                        break
        self.failed.add(isrc)
        raise ValueError(
            f"Giving up on Spotify ISRC {isrc}; leaving its fields unchanged"
        )
