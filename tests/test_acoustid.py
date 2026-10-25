import json
import logging
import shlex
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, urlsplit

import pytest
from ruamel.yaml import YAML

from mediascripts.acoustid import acoustid

KEY = "private+key/with spaces"
CORE = "# preserve me\ntrackData:\n  title: Track\n  artist: Artist\n"
FINGERPRINT = (
    "trackIdData:\n  - spotifyId: existing\n    fpcalcData:\n      duration: 203.75\n      fingerprint: AQADtE-example\n"
)
RESULT = {
    "status": "ok",
    "results": [
        {"id": "match-1", "score": 0.9, "recordings": [{"id": "recording-1"}, {"id": "recording-2"}]},
        {"id": "match-2", "score": 0.8, "recordings": [{"id": "recording-1"}]},
    ],
}


class HttpResponse(BytesIO):
    status = 200


@pytest.fixture(autouse=True)
def isolate_proposals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    create_directory = acoustid.tempfile.mkdtemp

    def make_directory(*, prefix: str, dir: str) -> str:
        assert dir == "/tmp"
        return create_directory(prefix=prefix, dir=tmp_path)

    monkeypatch.setattr(acoustid.tempfile, "mkdtemp", make_directory)


@pytest.fixture
def track(tmp_path: Path) -> Path:
    path = tmp_path / "01 - Track.yml"
    path.write_text(CORE + FINGERPRINT)
    path.chmod(0o640)
    return path


@pytest.fixture
def http_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock(return_value=HttpResponse(json.dumps(RESULT).encode()))
    monkeypatch.setattr(acoustid, "urlopen", mock)
    monkeypatch.setattr(acoustid.time, "sleep", MagicMock())
    return mock


@pytest.mark.parametrize("dry_run", [False, True])
def test_lookup_saves_full_response_or_leaves_dry_run_untouched(
    track: Path,
    http_mock: MagicMock,
    dry_run: bool,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = track.read_bytes()
    with caplog.at_level(logging.INFO, logger=acoustid.__name__):
        assert acoustid.run(KEY, specific_file=track, dry_run=dry_run)
    params = parse_qs(urlsplit(http_mock.call_args.args[0].full_url).query)
    assert params == {
        "client": [KEY],
        "duration": ["203"],
        "fingerprint": ["AQADtE-example"],
        "meta": ["recordingids"],
        "format": ["json"],
    }
    assert http_mock.call_args.kwargs["timeout"] == 30
    assert "AcoustID raw response JSON:" in caplog.text
    assert "recording-1" in caplog.text
    assert KEY not in caplog.text
    assert quote_plus(KEY) not in caplog.text
    assert "[REDACTED]" in caplog.text
    backup = track.with_name(track.name + ".bak")
    command = shlex.split(capsys.readouterr().out.strip())
    assert command[0] == "meld"
    if dry_run:
        assert track.read_bytes() == original
        assert not backup.exists()
        assert command[1] == str(track.resolve())
        proposal = Path(command[2])
        assert proposal.parent.name.startswith("acoustid-proposal-")
        assert proposal.name == track.name
        document = YAML(typ="safe").load(proposal.read_text())
        assert document["trackIdData"][0]["acoustidData"] == RESULT
        assert KEY not in proposal.read_text()
    else:
        assert command[1:] == [str(backup.resolve()), str(track.resolve())]
        document = YAML(typ="safe").load(track.read_text())
        entry = document["trackIdData"][0]
        assert entry["acoustidData"] == RESULT
        assert "acoustidIds" not in entry
        assert "musicbrainzRecordingIds" not in entry
        assert entry["spotifyId"] == "existing"
        assert entry["fpcalcData"]["duration"] == 203.75
        assert entry["fpcalcData"]["fingerprint"] == "AQADtE-example"
        assert track.read_text().startswith("# preserve me")
        assert backup.read_bytes() == original
        assert track.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize(
    "ids",
    [
        "",
        "trackIdData: null\n",
        "trackIdData: []\n",
        "trackIdData:\n  - spotifyId: existing\n",
        "trackIdData:\n  - fpcalcData: {duration: 203}\n",
        "trackIdData:\n  - fpcalcData: {duration: 203, fingerprint: '  '}\n",
        "trackIdData:\n  - fpcalcData: {duration: 0, fingerprint: AQAA}\n",
        "trackIdData:\n  - fpcalcData: {duration: .nan, fingerprint: AQAA}\n",
        "trackIdData:\n  - fpcalcData: {duration: true, fingerprint: AQAA}\n",
    ],
)
def test_missing_fingerprint_or_duration_warns_without_request(
    track: Path,
    http_mock: MagicMock,
    ids: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    track.write_text(CORE + ids)
    original = track.read_bytes()
    assert acoustid.run(KEY, specific_file=track)
    http_mock.assert_not_called()
    assert "skipping" in caplog.text
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert track.read_bytes() == original


def test_count_limits_eligible_tracks_and_default_collection(
    tmp_path: Path,
    http_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "00 - missing.yml").write_text(CORE)
    for name in ("01.yml", "02.yaml", "03.yml"):
        (data / name).write_text(CORE + FINGERPRINT)
    (data / "artist.yml").write_text(CORE + FINGERPRINT)
    (data / "album.yml").write_text(CORE + FINGERPRINT)
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    http_mock.side_effect = lambda *args, **kwargs: HttpResponse(json.dumps(RESULT).encode())
    assert acoustid.run(KEY, count=2)
    assert http_mock.call_count == 2
    assert len(list(data.glob("*.bak"))) == 2
    assert not (data / "artist.yml.bak").exists()
    assert not (data / "03.yml.bak").exists()
    assert acoustid.time.sleep.call_count == 1


def test_zero_count_makes_no_requests(track: Path, http_mock: MagicMock) -> None:
    assert acoustid.run(KEY, specific_file=track, count=0)
    http_mock.assert_not_called()


def test_multiple_fingerprints_count_as_one_track_and_are_rate_limited(
    track: Path,
    http_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track.write_text(CORE + FINGERPRINT + "  - fpcalcData:\n      duration: 204\n      fingerprint: SECOND\n")
    http_mock.side_effect = lambda *args, **kwargs: HttpResponse(json.dumps(RESULT).encode())
    monkeypatch.setattr(acoustid.time, "monotonic", MagicMock(side_effect=[10, 10.1, 10.4]))
    assert acoustid.run(KEY, specific_file=track, count=1)
    assert http_mock.call_count == 2
    acoustid.time.sleep.assert_called_once()
    delay = acoustid.time.sleep.call_args.args[0]
    assert delay == pytest.approx(acoustid.REQUEST_INTERVAL - 0.1)
    assert delay > 1 / 3 - 0.1
    entries = YAML(typ="safe").load(track.read_text())["trackIdData"]
    assert all(entry["acoustidData"] == RESULT for entry in entries)


def test_error_on_one_track_does_not_stop_other_lookups(
    tmp_path: Path,
    http_mock: MagicMock,
) -> None:
    for name in ("01.yml", "02.yml"):
        (tmp_path / name).write_text(CORE + FINGERPRINT)
    http_mock.side_effect = [URLError("offline"), HttpResponse(json.dumps(RESULT).encode())]
    assert not acoustid.run(KEY, specific_dir=tmp_path)
    assert http_mock.call_count == 2
    assert not (tmp_path / "01.yml.bak").exists()
    assert (tmp_path / "02.yml.bak").exists()


@pytest.mark.parametrize("content", ["trackData: [", "[]", "trackIdData: []"])
def test_invalid_explicit_track_is_reported(
    track: Path,
    http_mock: MagicMock,
    content: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    track.write_text(content)
    assert not acoustid.run(KEY, specific_file=track)
    http_mock.assert_not_called()
    assert "Lookup failed" in caplog.text


def test_redaction_handles_escaped_values() -> None:
    key = "secret'\\\n+"
    for value in (key, repr([key]), json.dumps({"key": key}), quote_plus(key)):
        scrubbed = acoustid.redact(value, key)
        assert "[REDACTED]" in scrubbed
        assert key not in scrubbed


def test_dry_run_preserves_existing_backup_and_retains_unique_proposals(
    track: Path,
    http_mock: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup = track.with_name(track.name + ".bak")
    backup.write_text("previous backup")
    http_mock.side_effect = lambda *args, **kwargs: HttpResponse(json.dumps(RESULT).encode())
    proposals: list[Path] = []
    for _ in range(2):
        assert acoustid.run(KEY, specific_file=track, dry_run=True)
        proposals.append(Path(shlex.split(capsys.readouterr().out.strip())[2]))
    assert proposals[0] != proposals[1]
    assert all(proposal.is_file() for proposal in proposals)
    assert backup.read_text() == "previous backup"


@pytest.mark.parametrize("dry_run", [False, True])
def test_no_changes_create_no_proposal_or_meld_command(
    track: Path,
    http_mock: MagicMock,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    track.write_text(track.read_text() + "    acoustidData: " + json.dumps(RESULT) + "\n")
    original = track.read_bytes()
    assert acoustid.run(KEY, specific_file=track, dry_run=dry_run, force=True)
    http_mock.assert_called_once()
    assert track.read_bytes() == original
    assert capsys.readouterr().out == ""
    assert not list(track.parent.glob("acoustid-proposal-*"))
    assert not track.with_name(track.name + ".bak").exists()


@pytest.mark.parametrize("status", [200, 429])
@pytest.mark.parametrize(
    "body",
    [
        '{\n  "status" : "ok", "results" : [], "echo" : "' + KEY + '"\n}\n',
        '{"status": invalid JSON, "echo": "' + KEY + '"}',
    ],
)
def test_raw_response_logged_before_parsing(
    http_mock: MagicMock,
    caplog: pytest.LogCaptureFixture,
    status: int,
    body: str,
) -> None:
    if status == 429:
        http_mock.side_effect = HTTPError(
            acoustid.LOOKUP_URL,
            status,
            "Too many requests",
            None,
            BytesIO(body.encode()),
        )
    else:
        http_mock.return_value = HttpResponse(body.encode())
    with caplog.at_level(logging.INFO, logger=acoustid.__name__):
        if status != 200 or "invalid JSON" in body:
            with pytest.raises(ValueError):
                acoustid.lookup(KEY, 203, "AQADtE-example")
        else:
            acoustid.lookup(KEY, 203, "AQADtE-example")
    messages = [record.getMessage() for record in caplog.records]
    if "invalid JSON" in body:
        assert ("AcoustID response is not valid JSON; raw response body:\n" + body.replace(KEY, "[REDACTED]")) in messages
    else:
        formatted = json.dumps(json.loads(body), indent=2).replace(KEY, "[REDACTED]")
        assert f"AcoustID raw response JSON:\n{formatted}" in messages
    assert f"AcoustID response: HTTP {status}" in messages
    assert any("AcoustID request: GET https://" in message and "headers=" in message for message in messages)
    assert any("User-agent" in message and "timeout=30s" in message for message in messages)
    assert KEY not in caplog.text
    assert quote_plus(KEY) not in caplog.text


@pytest.mark.parametrize(
    "result",
    [
        {"status": "ok", "results": []},
        {"status": "ok", "results": [{"id": "match-1"}]},
    ],
)
def test_no_matches_or_unlinked_match(
    track: Path,
    http_mock: MagicMock,
    result: dict[str, object],
) -> None:
    original = track.read_bytes()
    http_mock.return_value = HttpResponse(json.dumps(result).encode())
    assert acoustid.run(KEY, specific_file=track)
    assert YAML(typ="safe").load(track.read_text())["trackIdData"][0]["acoustidData"] == result
    assert track.with_name(track.name + ".bak").read_bytes() == original


@pytest.mark.parametrize(
    "response",
    [
        b"invalid JSON",
        b"[]",
        b'{"status":"error","error":{"message":"bad client"}}',
        b'{"status":"ok","results":{}}',
        b'{"status":"ok","results":[{}]}',
        b'{"status":"ok","results":[{"id":"match","recordings":[{}]}]}',
    ],
)
def test_invalid_response_never_saves(
    track: Path,
    http_mock: MagicMock,
    response: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original = track.read_bytes()
    http_mock.return_value = HttpResponse(response)
    assert not acoustid.run(KEY, specific_file=track)
    assert track.read_bytes() == original
    assert not track.with_name(track.name + ".bak").exists()
    assert "Lookup failed" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        HTTPError(f"https://example.test/?client={KEY}", 429, KEY, None, None),
        URLError(f"failed https://example.test/?client={quote_plus(KEY)}"),
        TimeoutError(KEY),
    ],
)
def test_network_errors_never_expose_key(
    track: Path,
    http_mock: MagicMock,
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    http_mock.side_effect = error
    assert not acoustid.run(KEY, specific_file=track)
    assert KEY not in caplog.text
    assert quote_plus(KEY) not in caplog.text
    assert "Lookup failed" in caplog.text


def test_api_response_is_scrubbed_and_secret_ids_are_not_saved(
    track: Path,
    http_mock: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original = track.read_bytes()
    http_mock.return_value = HttpResponse(json.dumps({"status": "ok", "results": [{"id": KEY}]}).encode())
    with caplog.at_level(logging.INFO):
        assert not acoustid.run(KEY, specific_file=track)
    assert KEY not in caplog.text
    assert quote_plus(KEY) not in caplog.text
    assert track.read_bytes() == original


def test_only_acoustid_data_is_modified(track: Path, http_mock: MagicMock) -> None:
    track.write_text(
        CORE
        + FINGERPRINT
        + "    acoustidIds: [old-match, match-1]\n"
        + "    musicbrainzRecordingIds: [old-recording]\n"
        + "    acoustidData: {status: ok, results: []}\n"
        + "  - spotifyId: untouched\n"
    )
    original = YAML(typ="safe").load(track.read_text())
    assert acoustid.run(KEY, specific_file=track, force=True)
    updated = YAML(typ="safe").load(track.read_text())
    assert updated["trackIdData"][0].pop("acoustidData") == RESULT
    original["trackIdData"][0].pop("acoustidData")
    assert updated == original


def test_full_response_retains_additional_fields(track: Path, http_mock: MagicMock) -> None:
    response = {**RESULT, "extra": {"nested": [True, None, 42, "value"]}}
    http_mock.return_value = HttpResponse(json.dumps(response).encode())
    assert acoustid.run(KEY, specific_file=track)
    assert YAML(typ="safe").load(track.read_text())["trackIdData"][0]["acoustidData"] == response


def test_saved_response_loads_with_mediascan_schema(track: Path, http_mock: MagicMock) -> None:
    from mediascan.types.track.track_yaml_file_loader import load_track_yaml_file

    track.write_text(CORE + "  albumartist: Artist\n  album: Album\n  genre: Rock\n  year: 1995\n" + FINGERPRINT)
    assert acoustid.run(KEY, specific_file=track)
    loaded = load_track_yaml_file(track)
    assert loaded.track_id_data is not None
    data = loaded.track_id_data[0].acoustid_data
    assert data is not None
    assert data.status == "ok"
    assert data.results[0].id == "match-1"
    assert data.results[0].score == 0.9
    assert data.results[0].recordings[0].id == "recording-1"


def test_changed_source_is_not_overwritten(track: Path, http_mock: MagicMock) -> None:
    def lookup(*args: object, **kwargs: object) -> HttpResponse:
        track.write_text(CORE + "# concurrently edited\n")
        return HttpResponse(json.dumps(RESULT).encode())

    http_mock.side_effect = lookup
    assert not acoustid.run(KEY, specific_file=track)
    assert track.read_text() == CORE + "# concurrently edited\n"
    assert not track.with_name(track.name + ".bak").exists()


@pytest.mark.parametrize("arguments", [["--count", "-1"], ["--file", "a.yml", "--dir", "directory"]])
def test_invalid_cli_arguments(monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["acoustid", *arguments])
    with pytest.raises(SystemExit) as error:
        acoustid.main()
    assert error.value.code == 2


def test_cli_requires_key(track: Path, monkeypatch: pytest.MonkeyPatch, http_mock: MagicMock) -> None:
    monkeypatch.delenv("ACOUSTID_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["acoustid", "--file", str(track)])
    assert acoustid.main() == 1
    http_mock.assert_not_called()


def test_cli_dry_run(track: Path, monkeypatch: pytest.MonkeyPatch, http_mock: MagicMock) -> None:
    original = track.read_bytes()
    monkeypatch.setenv("ACOUSTID_API_KEY", KEY)
    monkeypatch.setattr(sys, "argv", ["acoustid", "--file", str(track), "-n", "-c", "1"])
    assert acoustid.main() == 0
    http_mock.assert_called_once()
    assert track.read_bytes() == original


@pytest.mark.parametrize("dry_run", [False, True])
def test_existing_acoustid_data_is_skipped_without_force(
    track: Path,
    http_mock: MagicMock,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    dry_run: bool,
) -> None:
    track.write_text(track.read_text() + "    acoustidData: {status: ok, results: []}\n")
    original = track.read_bytes()
    with caplog.at_level(logging.INFO):
        assert acoustid.run(KEY, specific_file=track, dry_run=dry_run)
    http_mock.assert_not_called()
    assert track.read_bytes() == original
    assert capsys.readouterr().out == ""
    assert "use --force" in caplog.text
    assert "No usable fpcalc fingerprint" not in caplog.text


@pytest.mark.parametrize("existing", ["{}", "null", "[]"])
def test_empty_acoustid_data_is_looked_up(track: Path, http_mock: MagicMock, existing: str) -> None:
    track.write_text(track.read_text() + "    acoustidData: " + existing + "\n")
    assert acoustid.run(KEY, specific_file=track)
    http_mock.assert_called_once()
    assert YAML(typ="safe").load(track.read_text())["trackIdData"][0]["acoustidData"] == RESULT


def test_force_looks_up_existing_acoustid_data(track: Path, http_mock: MagicMock) -> None:
    track.write_text(track.read_text() + "    acoustidData: {status: ok, results: []}\n")
    assert acoustid.run(KEY, specific_file=track, force=True)
    http_mock.assert_called_once()
    assert YAML(typ="safe").load(track.read_text())["trackIdData"][0]["acoustidData"] == RESULT


def test_skipped_tracks_do_not_consume_count(track: Path, http_mock: MagicMock) -> None:
    done = track.with_name("00-done.yml")
    done.write_text(track.read_text() + "    acoustidData: {status: ok, results: []}\n")
    assert acoustid.run(KEY, specific_dir=track.parent, count=1)
    http_mock.assert_called_once()
    assert YAML(typ="safe").load(track.read_text())["trackIdData"][0]["acoustidData"] == RESULT


def test_cli_force(track: Path, monkeypatch: pytest.MonkeyPatch, http_mock: MagicMock) -> None:
    track.write_text(track.read_text() + "    acoustidData: {status: ok, results: []}\n")
    monkeypatch.setenv("ACOUSTID_API_KEY", KEY)
    monkeypatch.setattr(sys, "argv", ["acoustid", "--file", str(track), "--force"])
    assert acoustid.main() == 0
    http_mock.assert_called_once()
