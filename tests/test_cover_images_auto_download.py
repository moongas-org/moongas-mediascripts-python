import logging
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import MagicMock

import pytest
from openai import OpenAI
from PIL import Image

from mediascripts.cover import cover_images_auto_download as download

if TYPE_CHECKING:
    from playwright.sync_api import Page


def make_image(fmt: str = "JPEG", size: tuple[int, int] = (100, 100)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=(128, 64, 32)).save(buffer, format=fmt)
    return buffer.getvalue()


SMALL_JPEG = make_image("JPEG", (100, 100))
LARGE_JPEG = make_image("JPEG", (1200, 1200))
LARGE_WEBP = make_image("WEBP", (1500, 1500))


@pytest.fixture
def album(tmp_path: Path) -> Path:
    directory = tmp_path / "data" / "Music" / "Artist_Name" / "Album_ Name [1999]"
    directory.mkdir(parents=True)
    return directory


def model_client(*contents: str) -> MagicMock:
    client = MagicMock(spec=OpenAI)
    responses: list[MagicMock] = []
    for content in contents:
        response = MagicMock()
        response.choices[0].message.content = content
        responses.append(response)
    client.chat.completions.create.side_effect = responses
    return client


def mock_downloads(monkeypatch: pytest.MonkeyPatch, images: dict[str, bytes]) -> MagicMock:
    def fake_download(url: str) -> bytes:
        if url not in images:
            raise ValueError(f"Unexpected URL: {url}")
        return images[url]

    mock = MagicMock(side_effect=fake_download)
    monkeypatch.setattr(download, "download_image", mock)
    return mock


def mock_browser(monkeypatch: pytest.MonkeyPatch, *urls: str) -> MagicMock:
    search = MagicMock(return_value=list(urls))

    @contextmanager
    def open_browser() -> Generator[download.CoverSearch, None, None]:
        yield search

    monkeypatch.setattr(download, "google_images_search", open_browser)
    return search


@pytest.fixture
def browser_page() -> Iterator["Page"]:
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error as error:
            if "Executable doesn't exist" not in str(error):
                raise
            pytest.skip("Run python -m playwright install chromium for browser tests")
        try:
            yield browser.new_page()
        finally:
            browser.close()


def test_find_cover_requires_basename_and_supported_extension(album: Path) -> None:
    (album / "cover.png").touch()
    (album / "front-cover.jpg").touch()
    assert download.find_cover(album) is None
    (album / "cover.webp").touch()
    assert download.find_cover(album) == album / "cover.webp"
    (album / "cover.jpg").touch()
    assert download.find_cover(album) == album / "cover.jpg"


def test_album_metadata_derives_and_unescapes_names(album: Path) -> None:
    assert download.album_metadata(album) == ("Artist.Name", "Album. Name", 1999)


def test_iter_album_dirs_finds_albums_without_descending(tmp_path: Path) -> None:
    album = tmp_path / "Music" / "Artist" / "Album [2001]"
    nested = album / "Nested Album [2002]"
    nested.mkdir(parents=True)
    (tmp_path / "Music" / "Not an album").mkdir()
    assert download.iter_album_dirs(tmp_path) == [album]


def test_google_images_url_sets_large_filter_and_encodes_metadata() -> None:
    url = urlsplit(download.google_images_url("A & B", "Album + Title", 1973))
    assert (url.scheme, url.netloc, url.path) == ("https", "www.google.com", "/search")
    assert parse_qs(url.query) == {
        "q": ["A & B Album + Title (1973 album) album cover art image"],
        "udm": ["2"],
        "tbs": ["isz:l"],
    }


def test_google_images_reads_original_urls_from_links_and_previews(browser_page: "Page") -> None:
    direct = "https://example.com/direct.jpg?x=1&y=2"
    preview = "https://example.com/preview.webp"
    result_url = "/imgres?" + urlencode({"imgurl": direct})
    thumb = "https://encrypted-tbn0.gstatic.com/images?q=thumbnail"
    url = download.google_images_url("Led Zeppelin", "Houses of the Holy", 1973)
    body = f"""<div data-ri="0"><a href="{result_url}"><img src="{thumb}"></a></div>
    <div data-ri="1"><a href="{result_url}"><img src="{thumb}"></a></div>
    <div data-ri="2"><img src="{thumb}" onclick="document.querySelector('.iPVvYb').src='{preview}'"></div>
    <img class="iPVvYb" src="{thumb}">
    """
    browser_page.route("**/*", lambda route: route.abort())
    browser_page.route(url, lambda route: route.fulfill(content_type="text/html", body=body))

    assert download.search_google_images(browser_page, "Led Zeppelin", "Houses of the Holy", 1973) == [direct, preview]


def test_google_images_detects_traffic_check(browser_page: "Page") -> None:
    url = download.google_images_url("A", "B", 2000)
    browser_page.route(url, lambda route: route.fulfill(
        content_type="text/html", body='<form id="captcha-form"></form>', status=429
    ))

    with pytest.raises(download.GoogleImagesError, match="unusual-traffic"):
        download.search_google_images(browser_page, "A", "B", 2000)


def test_explicit_cover_retries_once_on_invalid_answer() -> None:
    client = model_client("I don't know.", "true")
    assert download.is_explicit_cover(cast(OpenAI, client), "A", "B", 2000)
    assert client.chat.completions.create.call_count == 2


def test_explicit_cover_rejects_invalid_answers() -> None:
    client = model_client("maybe", "unknown")
    with pytest.raises(ValueError, match="true or false"):
        download.is_explicit_cover(cast(OpenAI, client), "A", "B", 2000)


def test_choose_image_prefers_jpeg_then_resolution() -> None:
    data, is_jpeg = download.choose_image([LARGE_WEBP, SMALL_JPEG])
    assert is_jpeg and data == SMALL_JPEG
    data, is_jpeg = download.choose_image([SMALL_JPEG, LARGE_JPEG])
    assert is_jpeg and data == LARGE_JPEG
    with pytest.raises(ValueError, match="No readable"):
        download.choose_image([b"not an image"])


def test_process_album_downloads_and_saves_jpeg(
    album: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_downloads(monkeypatch, {"https://example.com/big.jpg": LARGE_JPEG})

    assert download.process_album(album, lambda *_: ["https://example.com/big.jpg"])

    cover = album / "cover.jpg"
    assert cover.read_bytes() == LARGE_JPEG
    assert not (album / "cover.jpg.bak").exists()


def test_process_album_converts_non_jpeg(
    album: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_downloads(monkeypatch, {"https://example.com/big.webp": LARGE_WEBP})

    assert download.process_album(album, lambda *_: ["https://example.com/big.webp"])

    with Image.open(album / "cover.jpg") as image:
        assert image.format == "JPEG"
        assert image.size == (1500, 1500)


def test_process_album_skips_existing_cover_without_force(album: Path) -> None:
    (album / "cover.jpg").write_bytes(SMALL_JPEG)
    search = MagicMock()
    assert download.process_album(album, search)
    search.assert_not_called()


def test_process_album_force_replaces_cover_with_backup(
    album: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (album / "cover.jpg").write_bytes(SMALL_JPEG)
    mock_downloads(monkeypatch, {"https://example.com/big.jpg": LARGE_JPEG})

    assert download.process_album(album, lambda *_: ["https://example.com/big.jpg"], force=True)
    assert (album / "cover.jpg").read_bytes() == LARGE_JPEG
    assert (album / "cover.jpg.bak").read_bytes() == SMALL_JPEG


def test_process_album_continues_past_failed_downloads(
    album: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = ["https://example.com/bad.jpg", "https://example.com/good.jpg"]
    images = {"https://example.com/good.jpg": LARGE_JPEG}

    def fake_download(url: str) -> bytes:
        if url not in images:
            raise ValueError("gone")
        return images[url]

    monkeypatch.setattr(download, "download_image", fake_download)
    assert download.process_album(album, lambda *_: urls)
    assert (album / "cover.jpg").read_bytes() == LARGE_JPEG


def test_process_album_dry_run_saves_nothing(
    album: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = model_client("true")
    monkeypatch.setattr(download, "get_client", lambda: cast(OpenAI, client))
    mock_downloads(monkeypatch, {"https://example.com/big.jpg": LARGE_JPEG})
    (album / "album.yml").write_text(
        "albumData:\n  name: Album. Name\n  explicitCover: false\n"
        "  dateReleased:\n    y: 1999\n"
    )

    assert download.process_album(album, lambda *_: ["https://example.com/big.jpg"], dry_run=True)
    client.chat.completions.create.assert_called_once()
    assert not (album / "cover.jpg").exists()
    assert "explicitCover: false" in (album / "album.yml").read_text()
    assert not (album / "album.yml.bak").exists()


def test_explicit_cover_sets_flag_and_preserves_other_content(album: Path) -> None:
    album_yml = album / "album.yml"
    album_yml.write_text(
        "# keep this comment\nalbumData:\n  name: Album. Name\n  explicitCover: false\n"
        "  dateReleased:\n    y: 1999\n"
    )
    assert download.set_explicit_cover(album, dry_run=False)
    text = album_yml.read_text()
    assert "# keep this comment" in text
    assert "explicitCover: true" in text
    assert "explicitCover: false" in (album / "album.yml.bak").read_text()
    # Already set: no second change, no new backup
    assert not download.set_explicit_cover(album, dry_run=False)


def test_explicit_cover_without_album_yml_is_a_no_op(album: Path) -> None:
    assert not download.set_explicit_cover(album, dry_run=False)
    assert not (album / "album.yml").exists()


def test_main_loop_processes_only_missing_covers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_cover = tmp_path / "data" / "Music" / "Artist" / "Done [2000]"
    missing = tmp_path / "data" / "Music" / "Artist" / "Missing [2001]"
    with_cover.mkdir(parents=True)
    missing.mkdir(parents=True)
    (with_cover / "cover.webp").touch()
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    search = mock_browser(monkeypatch, "https://example.com/x.jpg")
    mock_downloads(monkeypatch, {"https://example.com/x.jpg": LARGE_JPEG})

    assert download.main_loop(0)

    assert (missing / "cover.jpg").exists()
    search.assert_called_once_with("Artist", "Missing", 2001)


def test_main_loop_llm_disabled_reports_missing_without_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "data" / "Music" / "Artist" / "Missing [2001]").mkdir(parents=True)
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))

    def fail_browser() -> None:
        pytest.fail("Local-only mode must not start a browser")

    monkeypatch.setattr(download, "google_images_search", fail_browser)
    with caplog.at_level(logging.INFO):
        assert not download.main_loop(0, llm_enabled=False)
    assert "1 of 1 album(s) are missing" in caplog.text
    assert not list(tmp_path.rglob("cover.*"))


def test_main_loop_count_limits_and_specific_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for year in (2000, 2001, 2002):
        (tmp_path / "data" / "Music" / "Artist" / f"Album {year} [{year}]").mkdir(parents=True)
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    search = mock_browser(monkeypatch, "https://example.com/x.jpg")
    mock_downloads(monkeypatch, {"https://example.com/x.jpg": LARGE_JPEG})

    assert download.main_loop(0, count=2)
    assert search.call_count == 2

    with pytest.raises(ValueError, match="does not exist"):
        download.main_loop(0, specific_dir=tmp_path / "missing")


def test_main_loop_requires_collection_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOONGAS_COLLECTION_ROOTDIR", raising=False)
    with pytest.raises(ValueError, match="MOONGAS_COLLECTION_ROOTDIR"):
        download.main_loop(0)


def test_main_loop_stops_batch_when_google_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    for year in (2000, 2001):
        (tmp_path / "data" / "Music" / "Artist" / f"Album {year} [{year}]").mkdir(parents=True)
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    search = mock_browser(monkeypatch)
    search.side_effect = download.GoogleImagesError("blocked")

    assert not download.main_loop(0, quiet=True)
    search.assert_called_once()
    assert "Cover search stopped after 0 of 2 album(s): blocked" in caplog.text
