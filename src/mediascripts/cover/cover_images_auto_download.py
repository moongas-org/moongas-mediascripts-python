"""Find and download cover images for albums that are missing one.

An album is missing a cover when its directory has no file with basename
"cover" and a supported extension (.jpg or .webp). Choose Google Images
with the Large filter or MusicBrainz with Cover Art Archive. The LLM only checks whether
the album's front cover is known to be explicit.
JPEG output is preferred; candidates of at least 1000x1000 pixels are
preferred. Non-JPEG downloads are converted to cover.jpg. If the cover is
explicit and the album directory contains album.yml, explicitCover is set
to true there (the original is backed up to album.yml.bak).

Usage: cover-images-auto-download [options]
"""

import argparse
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

from mediascan.types.album.album_path import AlbumPathBuilder
from openai import APIError, APIStatusError, OpenAI
from openai.types.chat import ChatCompletionMessageParam
from PIL import Image, UnidentifiedImageError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from tqdm import tqdm

from mediascripts.common.cli.parse_boolean import parse_boolean
from mediascripts.common.http import retry_after_seconds
from mediascripts.common.openai.client import MODEL_NAME, get_client
from mediascripts.mediafile.auto_tag_media_file import unescape_name
from mediascripts.track.track_yaml.track_yaml_common import TextWriter, save_yaml_atomically

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)

COVER_EXTENSIONS = {".jpg", ".webp"}
MAX_CANDIDATES = 5
MAX_SEARCH_RESULTS = 10
MAX_IMAGE_BYTES = 20_000_000
PREFERRED_MIN_RESOLUTION = 1000
YAML_ERRORS = (OSError, UnicodeError, YAMLError, ValueError, TypeError)
GOOGLE_IMAGE_RESULTS = "[data-ri]:has(img)"
GOOGLE_PREVIEW_IMAGES = "img.iPVvYb, img.sFlh5c.FyHeAf"
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/release-group"
COVER_ART_ARCHIVE_URL = "https://coverartarchive.org/release-group"
MUSICBRAINZ_USER_AGENT = "moongas-cover-download/1.0 (https://github.com/moongas-org/moongas-mediascripts-python)"
MUSICBRAINZ_MIN_INTERVAL = 1.1
MUSICBRAINZ_MAX_ATTEMPTS = 5
MUSICBRAINZ_MAX_RETRY_DELAY = 120.0
MUSICBRAINZ_RETRY_STATUSES = {429, 502, 503, 504}

SYSTEM_PROMPT = """Reply with only true or false. Reply true only if the requested album's
front cover artwork is known to show nudity, sexual imagery, graphic violence, or
a parental-advisory label. Reply false if unsure. Do not return image URLs.
"""

CoverSearch = Callable[[str, str, int], list[str]]


class GoogleImagesError(RuntimeError):
    """Google Images is unavailable; stop the batch rather than retrying every album."""


class MusicBrainzError(RuntimeError):
    """MusicBrainz is throttling requests or unavailable."""


class _YamlRoundTrip(Protocol):
    def load(self, stream: Path) -> object: ...
    def dump(self, data: object, stream: TextWriter) -> None: ...


def find_cover(album_dir: Path) -> Path | None:
    """Return the existing cover image in an album directory, if any."""
    for path in sorted(album_dir.iterdir()):
        if path.is_file() and path.stem == "cover" and path.suffix.lower() in COVER_EXTENSIONS:
            return path
    return None


def album_metadata(album_dir: Path) -> tuple[str, str, int]:
    """Derive (artist, album, year) from a valid mediascan album directory."""
    album_path = AlbumPathBuilder.of(album_dir)
    if not album_path.valid:
        raise ValueError(f"Not a valid mediascan album directory: {album_dir}")
    artist = unescape_name(album_path.artist_dirname)
    album = unescape_name(re.sub(r"\s*\[\d{4}\]\s*$", "", album_path.album_dirname)).strip()
    if not artist or not album:
        raise ValueError(f"Cannot derive artist/album names from {album_dir}")
    return artist, album, album_path.year


def iter_album_dirs(root: Path) -> list[Path]:
    """Find all valid album directories beneath root, without descending into them."""
    albums: list[Path] = []
    for directory, subdirs, _ in os.walk(root, followlinks=True):
        album_dir = Path(directory)
        if AlbumPathBuilder.of(album_dir).valid:
            albums.append(album_dir)
            subdirs.clear()
    return sorted(albums)


def google_images_url(artist: str, album: str, year: int) -> str:
    query = f"{artist} {album} ({year} album) album cover art image"
    return "https://www.google.com/search?" + urlencode({"q": query, "udm": "2", "tbs": "isz:l"})


def original_image_url(src: str) -> bool:
    parsed = urlsplit(src)
    host = parsed.hostname or ""
    return parsed.scheme in {"http", "https"} and bool(host) and not host.startswith("encrypted-tbn")


def check_google_images_page(page: "Page") -> None:
    location = urlsplit(page.url)
    if location.path.startswith("/sorry/") or page.locator("#captcha-form").count():
        raise GoogleImagesError("Google Images blocked the browser with an unusual-traffic check")
    if location.hostname == "consent.google.com":
        raise GoogleImagesError("Google Images redirected the browser to a consent page")


def search_google_images(page: "Page", artist: str, album: str, year: int) -> list[str]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    url = google_images_url(artist, album, year)
    logger.info("Searching Google Images (Large): %s", url)
    response = page.goto(url, wait_until="domcontentloaded")
    check_google_images_page(page)
    if response is not None and not response.ok:
        raise GoogleImagesError(f"Google Images returned HTTP {response.status}")
    results = page.locator(GOOGLE_IMAGE_RESULTS)
    try:
        results.first.wait_for(timeout=10_000)
    except PlaywrightTimeout as error:
        check_google_images_page(page)
        if page.get_by_text("No results found", exact=False).first.is_visible():
            return []
        raise GoogleImagesError("Google Images did not show a recognizable results grid") from error

    urls: list[str] = []
    preview = page.locator(GOOGLE_PREVIEW_IMAGES)
    for result in results.all()[:MAX_SEARCH_RESULTS]:
        link = result.locator('a[href*="imgurl="]').first
        href = link.get_attribute("href") if link.count() else None
        direct_urls = parse_qs(urlsplit(href).query).get("imgurl", []) if href else []
        if not any(original_image_url(src) for src in direct_urls):
            previous = cast(list[str], preview.evaluate_all("(images) => images.map(img => img.currentSrc)"))
            try:
                result.locator("img").first.click(timeout=3_000)
                # The preview initially shows the result thumbnail (or the previous selection).
                handle = page.wait_for_function(
                    """({selector, previous}) => Array.from(document.querySelectorAll(selector))
                        .map(img => img.currentSrc)
                        .find(src => /^https?:/.test(src) && !new URL(src).hostname.startsWith('encrypted-tbn')
                            && !previous.includes(src))""",
                    arg={"selector": GOOGLE_PREVIEW_IMAGES, "previous": previous},
                    timeout=3_000,
                )
                direct_urls = [cast(str, handle.json_value())]
                handle.dispose()
            except PlaywrightTimeout:
                logger.debug("Google Images result did not expose a new original image URL")
        for src in direct_urls:
            if original_image_url(src) and src not in urls:
                urls.append(src)
        if len(urls) >= MAX_CANDIDATES:
            break
    check_google_images_page(page)
    return urls[:MAX_CANDIDATES]


@contextmanager
def google_images_search() -> Generator[tuple[CoverSearch, str], None, None]:
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError as error:
        raise GoogleImagesError("Install the cover-search extra and run `python -m playwright install chromium`") from error
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                user_agent: object = page.evaluate("navigator.userAgent")
                if not isinstance(user_agent, str) or not user_agent:
                    raise GoogleImagesError("Chromium did not provide a User-Agent")

                def search(artist: str, album: str, year: int) -> list[str]:
                    return search_google_images(page, artist, album, year)

                yield search, user_agent
            finally:
                browser.close()
    except PlaywrightError as error:
        raise GoogleImagesError(f"Chromium search failed: {error}") from error


def musicbrainz_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in decomposed if char.isalnum())


def musicbrainz_release_group(payload: object, artist: str, album: str, year: int) -> UUID | None:
    if not isinstance(payload, dict):
        raise ValueError("MusicBrainz returned an invalid release-group response")
    groups = cast(dict[str, object], payload).get("release-groups")
    if not isinstance(groups, list):
        raise ValueError("MusicBrainz response is missing its release-group list")
    artist_key, album_key = musicbrainz_name(artist), musicbrainz_name(album)
    matches: list[tuple[bool, UUID]] = []
    for group in cast(list[object], groups):
        if not isinstance(group, dict):
            continue
        fields = cast(dict[str, object], group)
        title, credits, mbid = fields.get("title"), fields.get("artist-credit"), fields.get("id")
        if not isinstance(title, str) or musicbrainz_name(title) != album_key:
            continue
        if not isinstance(credits, list) or not isinstance(mbid, str):
            continue
        credited_artist = ""
        for credit in cast(list[object], credits):
            if not isinstance(credit, dict):
                break
            credit_fields = cast(dict[str, object], credit)
            name, join = credit_fields.get("name"), credit_fields.get("joinphrase", "")
            if not isinstance(name, str) or not isinstance(join, str):
                break
            credited_artist += name + join
        else:
            if musicbrainz_name(credited_artist) != artist_key:
                continue
            date = fields.get("first-release-date")
            matches.append((isinstance(date, str) and date.split("-", 1)[0] == str(year), UUID(mbid)))
    # Prefer the requested year, keeping MusicBrainz's order for reissues.
    return max(matches, key=lambda match: match[0])[1] if matches else None


def musicbrainz_search() -> CoverSearch:
    next_request = 0.0

    def search(artist: str, album: str, year: int) -> list[str]:
        nonlocal next_request
        # Double quotes and backslashes must be escaped inside Lucene phrases.
        query = f"artist:{json.dumps(artist, ensure_ascii=False)} AND releasegroup:{json.dumps(album, ensure_ascii=False)}"
        url = MUSICBRAINZ_URL + "?" + urlencode({"query": query, "fmt": "json", "limit": 25})
        request = Request(url, headers={"User-Agent": MUSICBRAINZ_USER_AGENT, "Accept": "application/json"})
        for attempt in range(1, MUSICBRAINZ_MAX_ATTEMPTS + 1):
            delay = next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            logger.info(
                "MusicBrainz request: %s %s headers=%s (attempt %d/%d)",
                request.get_method(), request.full_url, json.dumps(dict(request.header_items())),
                attempt, MUSICBRAINZ_MAX_ATTEMPTS,
            )
            next_request = time.monotonic() + MUSICBRAINZ_MIN_INTERVAL
            try:
                with urlopen(request, timeout=30) as response:
                    payload: object = json.load(response)
                break
            except HTTPError as error:
                retry_after = error.headers.get("Retry-After")
                error.close()
                if error.code not in MUSICBRAINZ_RETRY_STATUSES:
                    raise
                if attempt == MUSICBRAINZ_MAX_ATTEMPTS:
                    raise MusicBrainzError(f"MusicBrainz returned HTTP {error.code} after {attempt} attempts") from error
                server_delay = retry_after_seconds(retry_after) if retry_after is not None else None
                if server_delay is not None and server_delay > MUSICBRAINZ_MAX_RETRY_DELAY:
                    raise MusicBrainzError(
                        f"MusicBrainz returned HTTP {error.code} and asked to retry in {server_delay:.0f}s, "
                        f"above the {MUSICBRAINZ_MAX_RETRY_DELAY:.0f}s limit; try again later"
                    ) from error
                retry_delay = max(2.0 ** attempt, server_delay if server_delay is not None else 0.0)
                logger.warning(
                    "MusicBrainz returned HTTP %d (attempt %d/%d); retrying in %.1fs",
                    error.code, attempt, MUSICBRAINZ_MAX_ATTEMPTS, retry_delay,
                )
                next_request = time.monotonic() + retry_delay
        else:
            raise AssertionError("MusicBrainz request loop exhausted")
        mbid = musicbrainz_release_group(payload, artist, album, year)
        if mbid is None:
            return []
        front = f"{COVER_ART_ARCHIVE_URL}/{mbid}/front"
        return [f"{front}-1200", front]

    return search


def is_explicit_cover(client: OpenAI, artist: str, album: str, year: int) -> bool:
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{artist} - {album} ({year})"},
    ]
    for attempt in range(2):
        response = client.chat.completions.create(model=MODEL_NAME, messages=messages, temperature=0.0)
        content = response.choices[0].message.content if response.choices else None
        answer = content.strip().lower() if content else ""
        if answer in {"true", "false"}:
            return answer == "true"
        if attempt == 0:
            logger.warning("Retrying LLM explicit-cover check: expected true or false")
    raise ValueError("Model did not return true or false for the explicit-cover check")


def download_image(url: str, user_agent: str) -> bytes:
    """Download one image URL with Chromium's User-Agent and a size cap."""
    if not re.match(r"^https?://", url):
        raise ValueError(f"Unsupported image URL: {url}")
    request = Request(url, headers={"User-Agent": user_agent})
    with urlopen(request, timeout=30) as response:
        data = response.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds {MAX_IMAGE_BYTES} bytes: {url}")
    return data


def choose_image(candidates: list[bytes], *, prefer_resolution: bool = False) -> tuple[bytes, bool]:
    """Return (image bytes, is_jpeg) for the highest-ranked candidate."""
    scored: list[tuple[tuple[int, bool, bool, int], bytes, bool]] = []
    for data in candidates:
        try:
            with Image.open(BytesIO(data)) as image:
                is_jpeg = image.format == "JPEG"
                width, height = image.size
        except (UnidentifiedImageError, OSError) as error:
            logger.warning("Ignoring unreadable image candidate: %s", error)
            continue
        pixels = width * height
        score = (pixels if prefer_resolution else 0, is_jpeg, min(width, height) >= PREFERRED_MIN_RESOLUTION, pixels)
        scored.append((score, data, is_jpeg))
    if not scored:
        raise ValueError("No readable cover image candidates")
    _, data, is_jpeg = max(scored, key=lambda item: item[0])
    return data, is_jpeg


def save_cover(album_dir: Path, data: bytes, is_jpeg: bool, dry_run: bool) -> Path:
    """Save cover.jpg atomically, converting non-JPEG images; back up a replaced cover."""
    destination = album_dir / "cover.jpg"
    if is_jpeg:
        output = data
    else:
        with Image.open(BytesIO(data)) as image:
            buffer = BytesIO()
            image.convert("RGB").save(buffer, format="JPEG", quality=95)
            output = buffer.getvalue()
    if dry_run:
        logger.info("[%s] [dry-run] Would save %d bytes to %s", album_dir, len(output), destination)
        return destination
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=album_dir, prefix=".cover.jpg.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(output)
        if destination.exists():
            destination.replace(album_dir / "cover.jpg.bak")
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    logger.info("[%s] Saved cover image: %s", album_dir, destination)
    return destination


def set_explicit_cover(album_dir: Path, dry_run: bool) -> bool:
    """Set explicitCover: true in album.yml if the file exists and the flag is not set."""
    album_yaml = album_dir / "album.yml"
    if not album_yaml.is_file():
        logger.debug("[%s] No album.yml; cannot record explicit cover", album_dir)
        return False
    yaml = YAML()
    yaml.preserve_quotes = True
    document = cast(dict[str, object], cast(_YamlRoundTrip, yaml).load(album_yaml))
    album_data = document.get("albumData")
    if not isinstance(album_data, dict):
        raise ValueError(f"albumData must be a mapping: {album_yaml}")
    album_data = cast(dict[str, object], album_data)
    if album_data.get("explicitCover"):
        logger.info("[%s] album.yml already has explicitCover: true", album_dir)
        return False
    if dry_run:
        logger.info("[%s] [dry-run] Would set explicitCover: true in %s", album_dir, album_yaml)
        return True
    album_data["explicitCover"] = True

    def write(stream: TextWriter) -> None:
        cast(_YamlRoundTrip, yaml).dump(document, stream)

    save_yaml_atomically(album_yaml, write, album_yaml.with_name("album.yml.bak"))
    logger.info("[%s] Set explicitCover: true in %s", album_dir, album_yaml)
    return True


def process_album(
    album_dir: Path, search: CoverSearch, dry_run: bool = False, force: bool = False, *,
    user_agent: str, explicit_check: bool = True, prefer_resolution: bool = False,
) -> bool:
    """Download a cover for one album directory. Returns True when nothing more is needed."""
    try:
        cover = find_cover(album_dir)
        if cover is not None and not force:
            logger.debug("[%s] Cover already exists: %s", album_dir, cover)
            return True
        artist, album, year = album_metadata(album_dir)
        urls = search(artist, album, year)
        if not urls:
            logger.warning("[%s] No matching cover image URLs found for %r - %r", album_dir, artist, album)
            return False
        candidates: list[bytes] = []
        for url in urls:
            try:
                candidates.append(download_image(url, user_agent))
            except (OSError, ValueError, URLError, HTTPError) as error:
                logger.warning("[%s] Could not download %s: %s", album_dir, url, error)
        if not candidates:
            logger.warning("[%s] No downloadable cover candidates for %r - %r", album_dir, artist, album)
            return False
        data, is_jpeg = choose_image(candidates, prefer_resolution=prefer_resolution)
        save_cover(album_dir, data, is_jpeg, dry_run)
        if explicit_check and (album_dir / "album.yml").is_file() and is_explicit_cover(get_client(), artist, album, year):
            set_explicit_cover(album_dir, dry_run)
        return True
    except APIStatusError as error:
        logger.error(
            "[%s] Provider returned HTTP %d: %s. Check LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL.",
            album_dir, error.status_code, error,
        )
    except (APIError, *YAML_ERRORS) as error:
        logger.error("[%s] Could not download cover: %s", album_dir, error)
    return False


def main_loop(
    sleep: int,
    llm_enabled: bool = True,
    specific_dir: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
    count: int | None = None,
    quiet: bool = False,
    *,
    source: str = "google",
    explicit_check: bool = True,
) -> bool:
    if count is not None and count < 0:
        raise ValueError("count must be non-negative")
    if specific_dir is not None:
        if not specific_dir.is_dir():
            raise ValueError(f"Album directory does not exist: {specific_dir}")
        albums = [specific_dir]
    else:
        collection_root = os.environ.get("MOONGAS_COLLECTION_ROOTDIR")
        if not collection_root:
            raise ValueError("MOONGAS_COLLECTION_ROOTDIR must be set, or use --dir")
        data_path = Path(collection_root) / "data"
        if not data_path.is_dir():
            raise ValueError(f"Collection data directory does not exist: {data_path}")
        albums = iter_album_dirs(data_path)
    if not llm_enabled:
        missing = [album for album in albums if find_cover(album) is None]
        logger.info(
            "LLM lookup disabled; %d of %d album(s) are missing cover images",
            len(missing), len(albums),
        )
        return not missing
    pending = [album for album in albums if force or find_cover(album) is None]
    if count is not None:
        pending = pending[:count]
    logger.info("%d album(s) to process (of %d)", len(pending), len(albums))
    succeeded = 0
    failed = 0
    if pending:
        if source == "musicbrainz":
            lookup = nullcontext((musicbrainz_search(), MUSICBRAINZ_USER_AGENT))
        elif source == "google":
            lookup = google_images_search()
        else:
            raise ValueError(f"Unknown cover source: {source}")
        try:
            with lookup as (search, user_agent):
                for index, album in enumerate(tqdm(pending, desc="Downloading covers", unit="album", disable=quiet)):
                    if process_album(
                        album, search, dry_run=dry_run, force=force, user_agent=user_agent, explicit_check=explicit_check,
                        prefer_resolution=source == "musicbrainz",
                    ):
                        succeeded += 1
                    else:
                        failed += 1
                    if sleep and index + 1 < len(pending):
                        time.sleep(sleep)
        except (GoogleImagesError, MusicBrainzError) as error:
            logger.error("Cover search stopped after %d of %d album(s): %s", succeeded + failed, len(pending), error)
            return False
    logger.info("Cover download complete: %d succeeded, %d failed", succeeded, failed)
    return failed == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-d", "--dir", type=Path,
        help="Process one album directory instead of scanning the collection.",
    )
    parser.add_argument(
        "--sleep", type=int, default=30, help="Seconds between albums (default: 30).",
    )
    parser.add_argument(
        "--llm", type=parse_boolean, default=True, metavar="true|false",
        help="Enable cover lookup (default: true). "
        "Use --llm=false to only report missing covers.",
    )
    parser.add_argument(
        "--source", choices=("google", "musicbrainz"), default="google",
        help="Cover search backend (default: google). MusicBrainz uses Cover Art Archive without Chromium.",
    )
    parser.add_argument(
        "--no-explicit-check", action="store_true",
        help="Download covers without calling the LLM or updating explicitCover.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Also process albums that already have a cover image.",
    )
    parser.add_argument(
        "-c", "--count", type=int, metavar="VALUE",
        help="Process at most VALUE albums missing covers (non-negative; default: unlimited).",
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="Search and download without saving images or album.yml changes.",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress per-album progress notices; keep summaries, warnings, and errors.",
    )
    args = parser.parse_args()
    if args.sleep < 0:
        parser.error("--sleep must be non-negative")
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return 0 if main_loop(
            args.sleep, args.llm, args.dir, args.force, args.dry_run, args.count, args.quiet,
            source=args.source, explicit_check=not args.no_explicit_check,
        ) else 1
    except YAML_ERRORS as error:
        logger.error("%s", error)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
