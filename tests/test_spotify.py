import logging
import shlex
import sys
from copy import deepcopy
from pathlib import Path
from typing import Protocol, TypedDict, cast
from unittest.mock import MagicMock

import pytest
import requests
from ruamel.yaml import YAML

from mediascripts.spotify import spotify


def test_get_access_token_returns_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    post = MagicMock()
    post.return_value.status_code = 200
    post.return_value.json.return_value = {"access_token": "token"}
    monkeypatch.setattr(spotify.requests, "post", post)
    assert spotify.get_access_token("client", "secret") == "token"
    assert post.call_args.kwargs["timeout"] == 30


@pytest.mark.parametrize("payload", [[], {}, {"access_token": None}, {"access_token": 123}])
def test_get_access_token_rejects_invalid_payload(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    post = MagicMock()
    post.return_value.status_code = 200
    post.return_value.json.return_value = payload
    monkeypatch.setattr(spotify.requests, "post", post)
    with pytest.raises(ValueError):
        spotify.get_access_token("client", "secret")


def test_get_track_by_isrc_returns_validated_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    get = MagicMock()
    get.return_value.status_code = 200
    get.return_value.json.return_value = RAW_API_RESPONSE
    monkeypatch.setattr(spotify.requests, "get", get)
    assert spotify.get_track_by_isrc("US123", "token") == [MATCH]
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer token"
    assert get.call_args.kwargs["params"] == {"q": "isrc:US123", "type": "track"}
    assert get.call_args.kwargs["timeout"] == 30


INVALID_SEARCH_PAYLOADS: list[object] = [
    [], {}, {"tracks": []}, {"tracks": {"items": {}}}, {"tracks": {"items": [None]}},
    {"tracks": {"items": [{"name": "Song", "id": 12, "external_urls": {"spotify": "url"}}]}},
    {"tracks": {"items": [{"name": "Song", "id": "id", "external_urls": {}}]}},
    {"tracks": {"items": [{"name": "Song", "id": None, "external_urls": {"spotify": "url"}}]}},
]


@pytest.mark.parametrize("payload", INVALID_SEARCH_PAYLOADS)
def test_get_track_by_isrc_rejects_invalid_payload(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    get = MagicMock()
    get.return_value.status_code = 200
    get.return_value.json.return_value = payload
    monkeypatch.setattr(spotify.requests, "get", get)
    with pytest.raises(ValueError):
        spotify.get_track_by_isrc("US123", "token")


CORE = "# preserve me\ntrackData:\n  title: Song\ncustomField: keep\n"
TRACK = (
    CORE + "trackIdData:\n"
    "  streamingData:\n"
    "      - streamingService: apple-music\n"
    "        trackUrl: https://music.apple.com/track/example\n"
    "        trackId: keep-this\n"
    "        apiData: null\n"
    "        isrc: USABC9500001\n"
    "mediaFileData:\n"
    "  - acoustidData:\n"
    "      status: ok\n"
    "      results:\n"
    "        - id: match\n"
    "          score: 0.9\n"
    "          recordings:\n"
    "            - id: recording\n"
    "              isrcs: [USABC9500001, USABC9500002]\n"
)
RAW_MATCH: dict[str, object] = {
    "name": "Song",
    "id": "track-id",
    "external_urls": {"spotify": "https://open.spotify.com/track/track-id"},
    "futureTrackField": {"preserved": True},
}
RAW_API_RESPONSE: dict[str, object] = {
    "tracks": {
        "href": "https://api.spotify.com/v1/search?query=isrc%3AUSABC9500001&type=track",
        "limit": 5,
        "next": None,
        "offset": 0,
        "previous": None,
        "total": 1,
        "items": [RAW_MATCH],
    },
    "futureResponseField": {"preserved": True},
}
MATCH: spotify.SpotifyTrack = {
    "name": "Song",
    "id": "track-id",
    "external_urls": {"spotify": "https://open.spotify.com/track/track-id"},
    "api_data": RAW_API_RESPONSE,
}


@pytest.fixture
def track(tmp_path: Path) -> Path:
    path = tmp_path / "01 - Track.yml"
    path.write_text(TRACK)
    return path


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SPOTIFY_API_CLIENT_ID", "client-credential")
    monkeypatch.setenv("SPOTIFY_API_CLIENT_SECRET", "secret-credential")
    monkeypatch.delenv("MOONGAS_COLLECTION_ROOTDIR", raising=False)
    make_directory = spotify.tempfile.mkdtemp

    def create_directory(*, prefix: str, dir: str) -> str:
        assert dir == "/tmp"
        return make_directory(prefix=prefix, dir=tmp_path)

    monkeypatch.setattr(spotify.tempfile, "mkdtemp", create_directory)
    monkeypatch.setattr(spotify.time, "sleep", MagicMock())


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    token = MagicMock(return_value="access-token")
    search = MagicMock(return_value=[deepcopy(MATCH)])
    monkeypatch.setattr(spotify, "get_access_token", token)
    monkeypatch.setattr(spotify, "get_track_by_isrc", search)
    return token, search


class YamlLoader(Protocol):
    def load(self, stream: str) -> object: ...


def load(path: Path) -> dict[str, object]:
    document = cast(YamlLoader, YAML(typ="safe")).load(path.read_text())
    assert spotify.is_mapping(document)
    return document


def entries(document: dict[str, object]) -> list[dict[str, object]]:
    items = document["trackIdData"]
    assert spotify.is_mapping(items)
    return [items]


def spotify_entry(entry: dict[str, object]) -> dict[str, object]:
    streaming_data = entry["streamingData"]
    assert spotify.is_list(streaming_data)
    for streaming in streaming_data:
        assert spotify.is_mapping(streaming)
        if streaming.get("streamingService") == "spotify":
            return streaming
    raise AssertionError("Spotify streaming data not found")


def test_updates_only_spotify_fields_and_preserves_backup(
    track: Path, api: tuple[MagicMock, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    before = load(track)
    assert spotify.run(specific_file=track)
    after = load(track)
    parent = entries(after)[0]
    entry = spotify_entry(parent)
    assert entry == {
        "streamingService": "spotify",
        "trackUrl": MATCH["external_urls"]["spotify"],
        "trackId": MATCH["id"],
        "apiData": RAW_API_RESPONSE,
        "isrc": "USABC9500001",
    }
    parent["streamingData"].remove(entry)  # type: ignore[union-attr]
    assert after == before
    assert track.read_text().startswith("# preserve me\n")
    backup = track.with_name(track.name + ".bak")
    assert backup.read_text() == TRACK
    assert shlex.split(capsys.readouterr().out) == ["meld", str(backup), str(track)]
    api[0].assert_called_once_with("client-credential", "secret-credential")
    api[1].assert_called_once_with("USABC9500001", "access-token")


def test_acoustid_isrcs_create_streaming_entry_when_track_ids_are_absent(
    tmp_path: Path, api: tuple[MagicMock, MagicMock]
) -> None:
    path = tmp_path / "01 - Media only.yml"
    path.write_text(
        CORE
        + "mediaFileData:\n"
        + "  - fpcalcData: {duration: 123, fingerprint: example}\n"
        + "    acoustidData:\n"
        + "      status: ok\n"
        + "      results: [{id: match, score: 0.9, recordings: [{id: recording, isrcs: [USABC9500001]}]}]\n"
    )
    assert spotify.run(specific_file=path)
    document = load(path)
    entry = spotify_entry(entries(document)[0])
    assert entry == {
        "streamingService": "spotify",
        "trackUrl": MATCH["external_urls"]["spotify"],
        "trackId": MATCH["id"],
        "apiData": RAW_API_RESPONSE,
        "isrc": "USABC9500001",
    }
    assert document["mediaFileData"][0]["fpcalcData"]["fingerprint"] == "example"


def test_dry_run_retains_proposal_and_does_not_touch_source_or_backup(
    track: Path,
    api: tuple[MagicMock, MagicMock],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    backup = track.with_name(track.name + ".bak")
    backup.write_text("existing backup")
    with caplog.at_level(logging.INFO):
        assert spotify.run(specific_file=track, dry_run=True)
    command = shlex.split(capsys.readouterr().out)
    proposal = Path(command[2])
    assert command[:2] == ["meld", str(track)]
    assert f"Diff command: meld {shlex.quote(str(track.resolve()))} {shlex.quote(str(proposal))}" in caplog.text
    assert spotify_entry(entries(load(proposal))[0])["trackId"] == "track-id"
    assert track.read_text() == TRACK
    assert backup.read_text() == "existing backup"


def test_offline_needs_no_credentials_and_never_calls_api(
    track: Path, api: tuple[MagicMock, MagicMock], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("SPOTIFY_API_CLIENT_ID")
    monkeypatch.delenv("SPOTIFY_API_CLIENT_SECRET")
    assert spotify.run(specific_file=track, dry_run=True, offline=True, overwrite=True)
    assert capsys.readouterr().out.splitlines() == [
        spotify.search_url("USABC9500001"), spotify.search_url("USABC9500002")
    ]
    for mock in api:
        mock.assert_not_called()
    assert track.read_text() == TRACK
    cast(MagicMock, spotify.time.sleep).assert_not_called()
    assert not list(track.parent.glob("spotify-proposal-*"))
    assert not track.with_name(track.name + ".bak").exists()


def test_nested_isrcs_and_first_result(track: Path, api: tuple[MagicMock, MagicMock]) -> None:
    track.write_text(TRACK.replace("        isrc: USABC9500001\n", "        isrc: ''\n"))
    second = deepcopy(MATCH)
    second["id"] = "second"
    api[1].return_value = [MATCH, second]
    assert spotify.run(specific_file=track)
    assert spotify_entry(entries(load(track))[0])["trackId"] == "track-id"
    api[1].assert_called_once_with("USABC9500001", "access-token")


def test_falls_back_to_next_isrc_after_no_match(track: Path, api: tuple[MagicMock, MagicMock]) -> None:
    api[1].side_effect = [[], [MATCH]]
    assert spotify.run(specific_file=track)
    assert api[1].call_count == 2
    assert spotify_entry(entries(load(track))[0])["trackId"] == "track-id"


def test_existing_complete_entry_skips_unless_forced(track: Path, api: tuple[MagicMock, MagicMock]) -> None:
    assert spotify.run(specific_file=track)
    original = track.read_text()
    api[1].reset_mock()
    assert spotify.run(specific_file=track, overwrite=True)
    api[1].assert_not_called()
    assert track.read_text() == original
    assert spotify.run(specific_file=track, force=True)
    assert track.read_text() == original
    assert track.with_name(track.name + ".bak").read_text() == TRACK
    assert spotify.run(specific_file=track, force=True, overwrite=True)
    assert track.with_name(track.name + ".bak").read_text() == original


@pytest.mark.parametrize("existing", ["track-id", "conflict"])
def test_incomplete_entries_preserve_existing_values_unless_forced(
    track: Path, api: tuple[MagicMock, MagicMock], existing: str
) -> None:
    track.write_text(
        TRACK.replace(
            "        isrc: USABC9500001\n",
            "        isrc: USABC9500001\n"
            "      - streamingService: spotify\n"
            "        trackUrl: existing-url\n"
            f"        trackId: {existing}\n"
            "        apiData: null\n"
            "        isrc: USABC9500001\n",
        )
    )
    original = track.read_text()
    assert spotify.run(specific_file=track)
    if existing == "conflict":
        assert track.read_text() == original
        assert spotify.run(specific_file=track, force=True)
        expected_url = MATCH["external_urls"]["spotify"]
    else:
        expected_url = "existing-url"
    assert spotify_entry(entries(load(track))[0])["trackUrl"] == expected_url


@pytest.mark.parametrize("source", ["isrc: ''", "isrc: null"])
def test_no_isrc_means_no_write(track: Path, api: tuple[MagicMock, MagicMock], source: str) -> None:
    track.write_text(
        CORE
        + "trackIdData:\n  streamingData:\n      - streamingService: spotify\n"
        + "        trackUrl: https://open.spotify.com/track/old\n"
        + "        trackId: old\n        apiData: null\n        "
        + source
        + "\n"
    )
    original = track.read_text()
    assert spotify.run(specific_file=track, overwrite=True)
    assert track.read_text() == original
    api[1].assert_not_called()


def test_no_match_does_not_clear_existing_fields(track: Path, api: tuple[MagicMock, MagicMock]) -> None:
    track.write_text(
        TRACK.replace(
            "        isrc: USABC9500001\n",
            "        isrc: USABC9500001\n"
            "      - streamingService: spotify\n"
            "        trackUrl: existing-url\n"
            "        trackId: existing\n"
            "        apiData: null\n"
            "        isrc: USABC9500001\n",
        )
    )
    original = track.read_text()
    api[1].return_value = []
    assert spotify.run(specific_file=track, force=True, overwrite=True)
    assert track.read_text() == original
    assert not track.with_name(track.name + ".bak").exists()


def test_collection_discovery_count_and_cache(
    tmp_path: Path, api: tuple[MagicMock, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    library = tmp_path / "library"
    data.mkdir()
    library.mkdir()
    (data / "Music").symlink_to(library, target_is_directory=True)
    (library / "artist.yml").write_text(TRACK)
    (library / "files.yml").write_text(TRACK)
    (library / "not-track.yml").write_text("something: else\n")
    first = library / "01.yml"
    second = library / "02.yaml"
    first.write_text(TRACK)
    second.write_text(TRACK)
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    assert spotify.run(count=1)
    assert sum("streamingService: spotify" in path.read_text() for path in (first, second)) == 1
    assert spotify.run()
    assert all("streamingService: spotify" in path.read_text() for path in (first, second))
    assert (library / "artist.yml").read_text() == TRACK
    api[1].reset_mock()
    assert spotify.run(specific_dir=library, force=True)
    api[1].assert_called_once()


def test_count_zero_does_not_authenticate(track: Path, api: tuple[MagicMock, MagicMock]) -> None:
    assert spotify.run(specific_file=track, count=0)
    api[0].assert_not_called()
    api[1].assert_not_called()


def test_retries_and_failure_cache(track: Path, api: tuple[MagicMock, MagicMock]) -> None:
    track.write_text(
        TRACK.replace(
            "mediaFileData:\n",
            "      - {streamingService: deezer, trackUrl: url, trackId: id, apiData: null, isrc: USABC9500001}\n"
            "mediaFileData:\n",
        )
    )
    api[1].side_effect = requests.ConnectionError("network unavailable")
    assert not spotify.run(specific_file=track)
    assert api[1].call_count == spotify.MAX_ATTEMPTS * 2
    assert not track.with_name(track.name + ".bak").exists()


def test_scalar_track_id_data_uses_first_successful_isrc(
    track: Path, api: tuple[MagicMock, MagicMock]
) -> None:
    track.write_text(
        CORE
        + "trackIdData:\n"
        + "  streamingData:\n"
        + "    - {streamingService: apple-music, trackUrl: url, trackId: one, apiData: null, isrc: USABC9500001}\n"
        + "    - {streamingService: deezer, trackUrl: url, trackId: two, apiData: null, isrc: USABC9500002}\n"
    )
    api[1].side_effect = [[], [MATCH]]
    assert spotify.run(specific_file=track)
    identifiers = entries(load(track))
    assert spotify_entry(identifiers[0])["trackId"] == "track-id"
    api[1].assert_has_calls([
        (("USABC9500001", "access-token"), {}),
        (("USABC9500002", "access-token"), {}),
    ])


@pytest.mark.parametrize("status", [401, 429, 503])
def test_http_retry_refresh_and_backoff(
    api: tuple[MagicMock, MagicMock], monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(spotify.time, "monotonic", lambda: 100.0)
    api[1].side_effect = [spotify.SpotifyHTTPError(status, 8), [MATCH]]
    client = spotify.SpotifyClient("client-credential", "secret-credential", spotify.MIN_SLEEP)
    assert client.lookup("USABC9500001") == [MATCH]
    assert api[0].call_count == (2 if status == 401 else 1)
    delays = [call.args[0] for call in cast(MagicMock, spotify.time.sleep).call_args_list]
    assert max(delays) >= (8 if status == 429 else spotify.MIN_SLEEP)
    if status == 503:
        assert max(delays) >= spotify.MIN_SLEEP + 0.5
        assert client.extra_sleep == 0.4
    assert client.lookup("USABC9500001") == [MATCH]
    assert api[1].call_count == 2


def test_authentication_retries(api: tuple[MagicMock, MagicMock]) -> None:
    api[0].side_effect = [requests.Timeout("timeout"), "access-token"]
    assert spotify.SpotifyClient("client-credential", "secret-credential", spotify.MIN_SLEEP).lookup("USABC9500001")
    assert api[0].call_count == 2


def test_permanent_http_error_is_not_retried(api: tuple[MagicMock, MagicMock]) -> None:
    api[1].side_effect = spotify.SpotifyHTTPError(403)
    with pytest.raises(ValueError, match="Giving up"):
        spotify.SpotifyClient("client-credential", "secret-credential", spotify.MIN_SLEEP).lookup("USABC9500001")
    api[1].assert_called_once()


def test_concurrent_edit_is_not_overwritten(track: Path, api: tuple[MagicMock, MagicMock]) -> None:
    def edit(*args: object) -> list[spotify.SpotifyTrack]:
        track.write_text(TRACK + "concurrent: change\n")
        return [MATCH]

    api[1].side_effect = edit
    assert not spotify.run(specific_file=track)
    assert track.read_text() == TRACK + "concurrent: change\n"
    assert not track.with_name(track.name + ".bak").exists()


def test_malformed_yaml_is_not_restored_or_repaired(track: Path, api: tuple[MagicMock, MagicMock]) -> None:
    backup = track.with_name(track.name + ".bak")
    backup.write_text(TRACK)
    track.write_text("trackData: *undefined\n")
    assert not spotify.run(specific_file=track)
    assert track.read_text() == "trackData: *undefined\n"
    assert backup.read_text() == TRACK
    api[1].assert_not_called()


@pytest.mark.parametrize("content", [
    "trackData: []\n",
    CORE + "trackIdData: []\n",
    CORE + "trackIdData: invalid\n",
    CORE + "trackIdData: {unknownId: value}\n",
    CORE + "trackIdData: {streamingData: [{streamingService: spotify, trackUrl: 12}]}\n",
    CORE + "mediaFileData: [{acoustidData: {results: [{recordings: [{isrcs: wrong}]}]}}]\n",
])
def test_invalid_track_schema_fails_without_writes(
    track: Path, api: tuple[MagicMock, MagicMock], content: str
) -> None:
    track.write_text(content)
    assert not spotify.run(specific_file=track)
    assert track.read_text() == content
    api[1].assert_not_called()


class RunOptions(TypedDict, total=False):
    count: int
    sleep: float
    offline: bool


INVALID_OPTIONS: list[RunOptions] = [
    {"count": -1}, {"sleep": 0}, {"sleep": float("nan")}, {"sleep": float("inf")}, {"offline": True},
]


@pytest.mark.parametrize("kwargs", INVALID_OPTIONS)
def test_invalid_options(kwargs: RunOptions) -> None:
    with pytest.raises(ValueError):
        spotify.run(**kwargs)


def test_missing_credentials(track: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPOTIFY_API_CLIENT_SECRET")
    with pytest.raises(ValueError, match="SPOTIFY_API_CLIENT_SECRET"):
        spotify.run(specific_file=track)


@pytest.mark.parametrize("args", [["--offline"], ["--count", "-1"], ["--sleep", "0"], ["--file", "a", "--dir", "b"]])
def test_cli_rejects_invalid_options(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["spotify", *args])
    with pytest.raises(SystemExit) as error:
        spotify.main()
    assert error.value.code == 2


def test_main_exit_status(track: Path, api: tuple[MagicMock, MagicMock], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["spotify", "--file", str(track)])
    assert spotify.main() == 0
    api[1].side_effect = ValueError("unavailable")
    monkeypatch.setattr(sys, "argv", ["spotify", "--file", str(track), "--force"])
    assert spotify.main() == 1


def test_authentication_body_and_secrets_are_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    post = MagicMock()
    post.return_value.status_code = 200
    post.return_value.json.return_value = {"access_token": "private-token"}
    monkeypatch.setattr(spotify.requests, "post", post)
    with caplog.at_level(logging.INFO):
        assert spotify.get_access_token("private-client", "private-secret") == "private-token"
    assert not any(secret in caplog.text for secret in ("private-client", "private-secret", "private-token"))


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_search_http_errors_are_not_successful_empty_results(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    response = requests.Response()
    response.status_code = status
    response._content = b'{"error": "failed"}'
    response.headers["Retry-After"] = "7"
    monkeypatch.setattr(spotify.requests, "get", MagicMock(return_value=response))
    with pytest.raises(spotify.SpotifyHTTPError) as error:
        spotify.get_track_by_isrc("USABC9500001", "access-token")
    assert error.value.status == status
    assert error.value.retry_after == (7 if status == 429 else 0)


def test_request_errors_redact_credentials(
    track: Path, api: tuple[MagicMock, MagicMock], caplog: pytest.LogCaptureFixture
) -> None:
    api[1].side_effect = requests.ConnectionError("client-credential secret-credential access-token")
    with caplog.at_level(logging.INFO):
        assert not spotify.run(specific_file=track)
    assert not any(secret in caplog.text for secret in ("client-credential", "secret-credential", "access-token"))


def test_real_request_flow(
    track: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    post = MagicMock()
    post.return_value.status_code = 200
    post.return_value.json.return_value = {"access_token": "private-token", "expires_in": 3600}
    get = MagicMock()
    get.return_value.status_code = 200
    get.return_value.json.return_value = RAW_API_RESPONSE
    monkeypatch.setattr(spotify.requests, "post", post)
    monkeypatch.setattr(spotify.requests, "get", get)
    with caplog.at_level(logging.INFO):
        assert spotify.run(specific_file=track)
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer private-token"
    assert spotify_entry(entries(load(track))[0])["trackId"] == "track-id"
    assert "private-token" not in caplog.text
    assert "client-credential" not in caplog.text
    assert "secret-credential" not in caplog.text
    assert "Spotify raw response JSON" in caplog.text


def test_normalizes_query_without_changing_isrcs(track: Path, api: tuple[MagicMock, MagicMock]) -> None:
    track.write_text(
        CORE
        + "trackIdData:\n  streamingData:\n      - {streamingService: apple-music, trackUrl: url, trackId: id, apiData: null, isrc: ' us-abc-95-00001 '}\n"
    )
    assert spotify.run(specific_file=track)
    api[1].assert_called_once_with("USABC9500001", "access-token")
    assert spotify_entry(entries(load(track))[0])["isrc"] == " us-abc-95-00001 "


def test_yaml_aliases_cannot_change_unrelated_fields(track: Path, api: tuple[MagicMock, MagicMock]) -> None:
    track.write_text(CORE + "customIdentifiers: &identifiers\n  streamingData: &entries\n"
                     "      - {streamingService: apple-music, trackUrl: url, trackId: id, apiData: null, isrc: USABC9500001}\n"
                     "customEntry: *entries\ntrackIdData: *identifiers\n")
    before = load(track)
    assert spotify.run(specific_file=track)
    after = load(track)
    assert after["customIdentifiers"] == before["customIdentifiers"]
    assert after["customEntry"] == before["customEntry"]
    assert spotify_entry(entries(after)[0])["trackId"] == "track-id"


def test_empty_results_are_cached(api: tuple[MagicMock, MagicMock]) -> None:
    api[1].return_value = []
    client = spotify.SpotifyClient("client-credential", "secret-credential", spotify.MIN_SLEEP)
    assert client.lookup("USABC9500001") == []
    assert client.lookup("USABC9500001") == []
    api[1].assert_called_once()


@pytest.mark.parametrize("header", ["NaN", "inf", "-1", "invalid"])
def test_invalid_retry_after_is_logged(header: str, caplog: pytest.LogCaptureFixture) -> None:
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = header
    assert spotify.retry_after_seconds(response) == 0
    assert "Invalid Spotify Retry-After" in caplog.text


def test_interrupted_track_has_no_partial_write(
    track: Path, api: tuple[MagicMock, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = (
        CORE + "trackIdData:\n"
        "  streamingData:\n"
        "    - {streamingService: apple-music, trackUrl: url, trackId: one, apiData: null, isrc: USABC9500001}\n"
        "    - {streamingService: deezer, trackUrl: url, trackId: two, apiData: null, isrc: USABC9500002}\n"
    )
    track.write_text(original)
    api[1].side_effect = [[], KeyboardInterrupt()]
    monkeypatch.setattr(sys, "argv", ["spotify", "--file", str(track)])
    assert spotify.main() == 130
    assert track.read_text() == original
    assert not track.with_name(track.name + ".bak").exists()


def test_backup_failure_preserves_source(
    track: Path, api: tuple[MagicMock, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mediascripts.track.track_yaml.track_yaml_common.shutil.copy2",
        MagicMock(side_effect=OSError("backup failed")),
    )
    assert not spotify.run(specific_file=track)
    assert track.read_text() == TRACK
    assert not list(track.parent.glob(".*.tmp"))
