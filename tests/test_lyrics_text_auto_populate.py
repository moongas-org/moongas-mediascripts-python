import logging
import sys
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from mediascripts.lyrics.lyrics_text import lyrics_text_auto_populate


def test_main_loop_shuffles_only_pending_lookups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracks = [tmp_path / f"{index:02} - Track.mp3" for index in range(1, 5)]
    for track in tracks:
        track.touch()
    tracks[0].with_suffix(".txt").touch()
    cache = {
        str(tracks[1].resolve()): {"status": lyrics_text_auto_populate.STATUS_INSTRUMENTAL}
    }
    monkeypatch.setattr(lyrics_text_auto_populate, "load_lookup_cache", lambda: cache)
    shuffled: list[Path] = []
    processed: list[Path] = []

    def shuffle(pending: list[Path]) -> None:
        shuffled.extend(pending)
        pending.reverse()

    def process(track: Path) -> str:
        processed.append(track)
        return lyrics_text_auto_populate.STATUS_ERROR

    monkeypatch.setattr(lyrics_text_auto_populate.random, "shuffle", shuffle)
    monkeypatch.setattr(lyrics_text_auto_populate, "process_track", process)
    monkeypatch.setattr(lyrics_text_auto_populate.time, "sleep", lambda seconds: None)

    lyrics_text_auto_populate.main_loop(0, specific_dir=tmp_path)

    assert shuffled == tracks[2:]
    assert processed == list(reversed(tracks[2:]))


@pytest.mark.parametrize("wrapped", [False, True])
def test_process_track_logs_timeout_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, wrapped: bool
) -> None:
    track = tmp_path / "01 - Track.mp3"
    track.touch()
    info = lyrics_text_auto_populate.TrackInfo("Artist", "Track", "Album", 180)
    monkeypatch.setattr(lyrics_text_auto_populate, "read_track_info", lambda path: info)

    def fail_request(*args: object, **kwargs: object) -> None:
        error = TimeoutError("The read operation timed out")
        if wrapped:
            raise URLError(error)
        raise error

    monkeypatch.setattr(lyrics_text_auto_populate, "urlopen", fail_request)
    with caplog.at_level(logging.ERROR):
        assert lyrics_text_auto_populate.process_track(track) == lyrics_text_auto_populate.STATUS_RETRY

    assert f"[{track.name}] LRCLIB request timed out; lookup failed" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert "Traceback" not in caplog.text
    assert not track.with_suffix(".txt").exists()


@pytest.mark.parametrize("code", [400, 429, 500, 503])
def test_process_track_logs_http_error_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, code: int
) -> None:
    track = tmp_path / "01 - Track.mp3"
    track.touch()
    info = lyrics_text_auto_populate.TrackInfo("Artist", "Track", "Album", 180)
    monkeypatch.setattr(lyrics_text_auto_populate, "read_track_info", lambda path: info)
    monkeypatch.setattr(lyrics_text_auto_populate, "LRCLIB_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(lyrics_text_auto_populate, "_request_delay_seconds", 0.5)

    def fail_request(*args: object, **kwargs: object) -> None:
        raise HTTPError("https://lrclib.net", code, "test error", Message(), None)

    monkeypatch.setattr(lyrics_text_auto_populate, "urlopen", fail_request)
    with caplog.at_level(logging.WARNING):
        expected = (
            lyrics_text_auto_populate.STATUS_ERROR if code == 400
            else lyrics_text_auto_populate.STATUS_RETRY
        )
        assert lyrics_text_auto_populate.process_track(track) == expected

    assert f"[{track.name}] LRCLIB returned HTTP {code}; lookup failed" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert "Traceback" not in caplog.text
    assert not track.with_suffix(".txt").exists()
    assert lyrics_text_auto_populate._request_delay_seconds == 1.0


@pytest.mark.parametrize("code", [400, 401, 404, 429, 499, 500, 503, 599])
def test_http_errors_increase_delay_even_on_final_attempt(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    monkeypatch.setattr(lyrics_text_auto_populate, "_request_delay_seconds", 0.5)
    monkeypatch.setattr(lyrics_text_auto_populate, "LRCLIB_MAX_ATTEMPTS", 1)

    def fail_request(*args: object, **kwargs: object) -> None:
        raise HTTPError("https://lrclib.net", code, "test error", Message(), None)

    monkeypatch.setattr(lyrics_text_auto_populate, "urlopen", fail_request)
    for expected_delay in (1.0, 1.5):
        if code == 404:
            assert lyrics_text_auto_populate.lrclib_request("/api/get", {}) is None
        else:
            with pytest.raises(HTTPError):
                lyrics_text_auto_populate.lrclib_request("/api/get", {})
        assert lyrics_text_auto_populate._request_delay_seconds == expected_delay


@pytest.mark.parametrize("retry_after", ["0", "10", "invalid"])
def test_request_does_not_retry_inline_and_records_retry_after(
    monkeypatch: pytest.MonkeyPatch, retry_after: str
) -> None:
    monkeypatch.setattr(lyrics_text_auto_populate, "_request_delay_seconds", 1.0)
    sleeps: list[float] = []
    monkeypatch.setattr(lyrics_text_auto_populate.time, "sleep", sleeps.append)
    headers = Message()
    headers["Retry-After"] = retry_after
    calls: list[int] = []

    def request(*args: object, **kwargs: object) -> BytesIO:
        calls.append(503)
        raise HTTPError("https://lrclib.net", 503, "test error", headers, None)

    monkeypatch.setattr(lyrics_text_auto_populate, "urlopen", request)
    with pytest.raises(HTTPError):
        lyrics_text_auto_populate.lrclib_request("/api/get", {})
    assert calls == [503]
    assert sleeps == []
    assert lyrics_text_auto_populate._retry_after_seconds == {
        "0": 0.0, "10": 10.0, "invalid": 5.0,
    }[retry_after]
    assert lyrics_text_auto_populate._request_delay_seconds == 1.5


def test_failed_tracks_go_to_back_of_queue_with_five_attempt_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tracks = [tmp_path / f"{index:02} - Track.mp3" for index in range(3)]
    for track in tracks:
        track.touch()
    monkeypatch.setattr(lyrics_text_auto_populate.random, "shuffle", lambda pending: None)
    monkeypatch.setattr(lyrics_text_auto_populate, "LOOKUP_CACHE_PATH", tmp_path / "cache.json")
    processed: list[Path] = []
    sleeps: list[float] = []
    monkeypatch.setattr(lyrics_text_auto_populate.time, "sleep", sleeps.append)

    def process(track: Path) -> str:
        processed.append(track)
        if track == tracks[0]:
            lyrics_text_auto_populate._retry_after_seconds = 10.0
            return lyrics_text_auto_populate.STATUS_RETRY
        if track == tracks[1] and processed.count(track) == 1:
            return lyrics_text_auto_populate.STATUS_RETRY
        return lyrics_text_auto_populate.STATUS_SAVED

    monkeypatch.setattr(lyrics_text_auto_populate, "process_track", process)
    with caplog.at_level(logging.INFO):
        lyrics_text_auto_populate.main_loop(0.5, specific_dir=tmp_path)

    assert processed == [
        tracks[0], tracks[1], tracks[2], tracks[0], tracks[1],
        tracks[0], tracks[0], tracks[0],
    ]
    assert sleeps == [10.0, 0.5, 0.5, 10.0, 0.5, 10.0, 10.0]
    assert "Giving up after 5 attempts this run" in caplog.text
    assert "Batch complete: 2 saved, 0 skipped, 0 instrumental, 0 not found, 1 errors" in caplog.text
    assert not (tmp_path / "cache.json").exists()


def test_every_request_logs_endpoint_parameters_and_success_or_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(lyrics_text_auto_populate.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(lyrics_text_auto_populate, "_request_delay_seconds", 0.5)

    def request(req: lyrics_text_auto_populate.Request, **kwargs: object) -> BytesIO:
        calls.append(req.full_url)
        if "/api/get?" in req.full_url:
            raise HTTPError(req.full_url, 404, "Not found", Message(), None)
        return BytesIO(b'[{"id": 123, "trackName": "Track", "plainLyrics": "Lyrics"}]')

    monkeypatch.setattr(lyrics_text_auto_populate, "urlopen", request)
    with caplog.at_level(logging.INFO):
        record = lyrics_text_auto_populate.lookup_lyrics(
            lyrics_text_auto_populate.TrackInfo("Artist", "Track", "Album", 180)
        )
    assert record is not None and record["id"] == 123
    assert len(calls) == 2
    assert "LRCLIB request: GET /api/get params=" in caplog.text
    assert "'artist_name': 'Artist'" in caplog.text
    assert "'duration': 180" in caplog.text
    assert "GET /api/get returned HTTP 404" in caplog.text
    assert "falling back to /api/search" in caplog.text
    assert "LRCLIB request: GET /api/search params=" in caplog.text
    assert "LRCLIB request completed: GET /api/search" in caplog.text


def test_http_failure_moves_to_next_track_before_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for title in ("First", "Second"):
        (tmp_path / f"{title}.mp3").touch()
    monkeypatch.setattr(lyrics_text_auto_populate.random, "shuffle", lambda pending: None)
    monkeypatch.setattr(lyrics_text_auto_populate, "LOOKUP_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(
        lyrics_text_auto_populate, "read_track_info",
        lambda path: lyrics_text_auto_populate.TrackInfo("Artist", path.stem, "Album", 180),
    )
    calls: list[str] = []
    sleeps: list[float] = []
    monkeypatch.setattr(lyrics_text_auto_populate.time, "sleep", sleeps.append)
    headers = Message()
    headers["Retry-After"] = "10"

    def request(req: lyrics_text_auto_populate.Request, **kwargs: object) -> BytesIO:
        calls.append(req.full_url)
        if len(calls) == 1:
            raise HTTPError(req.full_url, 503, "Unavailable", headers, None)
        return BytesIO(b'{"id": 123, "plainLyrics": "Lyrics"}')

    monkeypatch.setattr(lyrics_text_auto_populate, "urlopen", request)
    with caplog.at_level(logging.INFO):
        lyrics_text_auto_populate.main_loop(0.5, specific_dir=tmp_path)

    assert len(calls) == 3
    assert "track_name=First" in calls[0]
    assert "track_name=Second" in calls[1]
    assert "track_name=First" in calls[2]
    assert sleeps == [10.0, 1.0]
    assert (tmp_path / "First.txt").read_text(encoding="utf-8") == "Lyrics\n"
    assert (tmp_path / "Second.txt").read_text(encoding="utf-8") == "Lyrics\n"
    assert "Deferred until after 1 pending track(s)" in caplog.text
    assert "Batch complete: 2 saved, 0 skipped, 0 instrumental, 0 not found, 0 errors" in caplog.text
    assert "Traceback" not in caplog.text


def test_increased_delay_applies_to_search_and_next_track_and_resets_each_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("01 - First.mp3", "02 - Second.mp3"):
        (tmp_path / name).touch()
    monkeypatch.setattr(lyrics_text_auto_populate, "_request_delay_seconds", 99.0)
    monkeypatch.setattr(lyrics_text_auto_populate, "LOOKUP_CACHE_PATH", tmp_path / "cache.json")
    info = lyrics_text_auto_populate.TrackInfo("Artist", "Track", "Album", 180)
    monkeypatch.setattr(lyrics_text_auto_populate, "read_track_info", lambda path: info)
    sleeps: list[float] = []
    monkeypatch.setattr(lyrics_text_auto_populate.time, "sleep", sleeps.append)

    def request(*args: object, **kwargs: object) -> None:
        raise HTTPError("https://lrclib.net", 404, "Not found", Message(), None)

    monkeypatch.setattr(lyrics_text_auto_populate, "urlopen", request)
    for _ in range(2):
        sleeps.clear()
        lyrics_text_auto_populate.main_loop(0.5, specific_dir=tmp_path)
        assert sleeps == [1.0, 1.5, 2.0]
        assert lyrics_text_auto_populate._request_delay_seconds == 2.5


def test_process_track_skips_existing_lyrics_before_reading_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = tmp_path / "01 - Track.mp3"
    track.touch()
    lyrics = track.with_suffix(".txt")
    lyrics.write_text("Existing lyrics\n", encoding="utf-8")

    def unexpected_metadata_read(track_path: Path) -> None:
        pytest.fail("Existing lyrics must be checked before reading metadata or looking up lyrics")

    monkeypatch.setattr(
        lyrics_text_auto_populate, "read_track_info", unexpected_metadata_read
    )

    assert lyrics_text_auto_populate.process_track(track) == lyrics_text_auto_populate.STATUS_SKIPPED
    assert lyrics.read_text(encoding="utf-8") == "Existing lyrics\n"


@pytest.mark.parametrize("creation_stage", ["never", "lookup", "save"])
def test_process_track_saves_without_replacing_concurrently_created_lyrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, creation_stage: str
) -> None:
    track = tmp_path / "01 - Track.mp3"
    track.touch()
    lyrics = track.with_suffix(".txt")
    info = lyrics_text_auto_populate.TrackInfo("Artist", "Track", "Album", 180)
    monkeypatch.setattr(lyrics_text_auto_populate, "read_track_info", lambda path: info)

    def lookup(track_info: lyrics_text_auto_populate.TrackInfo) -> dict[str, object]:
        if creation_stage == "lookup":
            lyrics.write_text("Existing lyrics\n", encoding="utf-8")
        return {"id": 123, "plainLyrics": "New lyrics"}

    monkeypatch.setattr(lyrics_text_auto_populate, "lookup_lyrics", lookup)
    real_link = lyrics_text_auto_populate.os.link

    def link(source: Path, destination: Path) -> None:
        if creation_stage == "save":
            lyrics.write_text("Existing lyrics\n", encoding="utf-8")
        real_link(source, destination)

    monkeypatch.setattr(lyrics_text_auto_populate.os, "link", link)

    status = lyrics_text_auto_populate.process_track(track)

    if creation_stage == "never":
        assert status == lyrics_text_auto_populate.STATUS_SAVED
        assert lyrics.read_text(encoding="utf-8") == "New lyrics\n"
    else:
        assert status == lyrics_text_auto_populate.STATUS_SKIPPED
        assert lyrics.read_text(encoding="utf-8") == "Existing lyrics\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == [track.name, lyrics.name]


def test_main_loop_scans_only_missing_tracks_in_specific_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    album_dir = tmp_path / "album"
    album_dir.mkdir()
    track = album_dir / "01 - Track.mp3"
    track.touch()
    (album_dir / "02 - Existing.mp3").touch()
    (album_dir / "02 - Existing.txt").touch()
    nested_dir = album_dir / "disc 2"
    nested_dir.mkdir()
    (nested_dir / "01 - Nested.mp3").touch()
    monkeypatch.setattr(lyrics_text_auto_populate, "MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))

    with caplog.at_level(logging.INFO):
        lyrics_text_auto_populate.main_loop(
            sleep_between_requests=0,
            lookup_enabled=False,
            specific_dir=album_dir,
        )

    assert f"Missing: {track.with_suffix('.txt')}" in caplog.text
    assert "Existing.txt" not in caplog.text
    assert "Nested.mp3" not in caplog.text


def test_main_rejects_both_file_and_directory_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lyrics-text-auto-populate",
            "--file",
            str(tmp_path / "track.mp3"),
            "--dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit):
        lyrics_text_auto_populate.main()
