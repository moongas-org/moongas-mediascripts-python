import json
import logging
import shlex
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest
from ruamel.yaml import YAML

from mediascripts.acoustid import acoustid, musicbrainz

REC_1 = "0c652621-96d6-46bb-a886-b277b87bafd4"
REC_2 = "30cd7bd2-ac63-4dd7-a745-864d298f34cf"


@pytest.mark.parametrize("dry_run", [False, True])
def test_unparseable_track_recovers_backup_and_reprocesses(track: Path, http_mock: MagicMock, dry_run: bool) -> None:
    backup = track.with_name(track.name + ".bak")
    backup.write_text(TRACK)
    broken = TRACK.replace("isrcs: []", "isrcs: *undefined")
    track.write_text(broken)
    assert musicbrainz.run(specific_file=track, dry_run=dry_run)
    assert http_mock.call_count == 2
    if dry_run:
        assert track.read_text() == broken
        assert backup.read_text() == TRACK
    else:
        assert recordings(track)[0]["title"] == "Walking on Broken Glass"
        assert backup.read_text() == TRACK


def test_malformed_scalar_isrcs_are_looked_up(track: Path, http_mock: MagicMock) -> None:
    track.write_text(TRACK.replace("isrcs: []", 'isrcs: "*id001"'))
    assert musicbrainz.run(specific_file=track)
    assert recordings(track)[0]["isrcs"] == ["GBARL9200074", "USBM10255392"]


def test_invalid_id_still_allows_isrc_format_repair(track: Path, http_mock: MagicMock) -> None:
    track.write_text(TRACK.replace(REC_1, "invalid-one").replace(REC_2, "invalid-two").replace("isrcs: []", 'isrcs: "*id001"'))
    assert musicbrainz.run(specific_file=track)
    http_mock.assert_not_called()
    assert recordings(track)[0]["isrcs"] == []


def test_populated_unquoted_isrcs_are_left_unchanged(track: Path, http_mock: MagicMock) -> None:
    yaml = YAML()
    data = yaml.load(track.read_text())
    for result in data["mediaFileData"][0]["acoustidData"]["results"]:
        for recording in result["recordings"]:
            recording["isrcs"] = ["USSM18500120"]
    with track.open("w") as output:
        yaml.dump(data, output)
    original = track.read_text()
    assert musicbrainz.run(specific_file=track)
    http_mock.assert_not_called()
    assert track.read_text() == original
    assert not track.with_name(track.name + ".bak").exists()


CORE = "# preserve me\ntrackData:\n  title: Track\n  artist: Artist\n  albumartist: Artist\n  album: Album\n  genre: Rock\n  year: 1995\n"
TRACK = (
    CORE
    + "mediaFileData:\n"
    + "  - path: /music/existing.flac\n"
    + "    fpcalcData: {duration: 252, fingerprint: AQADtE-example}\n"
    + "    acoustidData:\n"
    + "      status: ok\n"
    + "      results:\n"
    + "        - id: match-1\n"
    + "          score: 0.98\n"
    + "          recordings:\n"
    + f"            - id: {REC_1}\n"
    + "              isrcs: []\n"
    + f"            - id: {REC_2}\n"
    + "        - id: match-2\n"
    + "          score: 0.5\n"
    + "          recordings:\n"
    + f"            - id: {REC_1}\n"
)
DETAILS: dict[str, dict[str, object]] = {
    REC_1: {
        "id": REC_1,
        "title": "Walking on Broken Glass",
        "isrcs": ["GBARL9200074", "USBM10255392"],
        "disambiguation": "",
        "first-release-date": "1992",
        "video": False,
        "length": 252000,
    },
    REC_2: {
        "id": REC_2,
        "title": "Walking on Broken Glass",
        "isrcs": [],
        "disambiguation": "live",
        "first-release-date": "1992-06-01",
        "video": True,
        "length": 260000,
    },
}
SAVED_1 = {
    "id": REC_1,
    "title": "Walking on Broken Glass",
    "length": 252000,
    "video": False,
    "disambiguation": "",
    "firstReleaseDate": "1992",
    "isrcs": ["GBARL9200074", "USBM10255392"],
}
SAVED_2 = {
    "id": REC_2,
    "title": "Walking on Broken Glass",
    "length": 260000,
    "video": True,
    "disambiguation": "live",
    "firstReleaseDate": "1992-06-01",
    "isrcs": [],
}


class HttpResponse(BytesIO):
    status = 200


def respond(request: Request, timeout: float) -> HttpResponse:
    recording_id = urlsplit(request.full_url).path.rsplit("/", 1)[-1]
    return HttpResponse(json.dumps(DETAILS[recording_id]).encode())


def recordings(path: Path) -> list[dict[str, object]]:
    data = YAML(typ="safe").load(path.read_text())["mediaFileData"][0]["acoustidData"]
    return [recording for result in data["results"] for recording in result["recordings"]]


@pytest.fixture(autouse=True)
def isolate_proposals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    create_directory = musicbrainz.tempfile.mkdtemp

    def make_directory(*, prefix: str, dir: str) -> str:
        assert dir == "/tmp"
        return create_directory(prefix=prefix, dir=tmp_path)

    monkeypatch.setattr(musicbrainz.tempfile, "mkdtemp", make_directory)


@pytest.fixture
def track(tmp_path: Path) -> Path:
    path = tmp_path / "01 - Track.yml"
    path.write_text(TRACK)
    return path


@pytest.fixture
def http_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock(side_effect=respond)
    monkeypatch.setattr(musicbrainz, "urlopen", mock)
    monkeypatch.setattr(musicbrainz.time, "sleep", MagicMock())
    return mock


def test_lookup_fills_recordings_and_dedupes_requests(
    track: Path, http_mock: MagicMock, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        assert musicbrainz.run(specific_file=track)
    assert http_mock.call_count == 2
    request: Request = http_mock.call_args_list[0].args[0]
    url = urlsplit(request.full_url)
    assert f"{url.scheme}://{url.netloc}{url.path}" == musicbrainz.RECORDING_URL + REC_1
    assert parse_qs(url.query) == {"inc": ["isrcs"], "fmt": ["json"]}
    assert "moongas-mediascripts" in request.get_header("User-agent")
    assert http_mock.call_args_list[0].kwargs["timeout"] == 30
    first, second, third = recordings(track)
    assert first == SAVED_1
    assert second == SAVED_2
    assert third == first
    text = track.read_text()
    assert "&id" not in text and "*id" not in text
    assert text.count("- GBARL9200074") == 2
    assert text.count("- USBM10255392") == 2
    assert text.startswith("# preserve me\n")
    assert "path: /music/existing.flac" in text and "fingerprint: AQADtE-example" in text
    backup = track.with_name(track.name + ".bak")
    assert backup.read_text() == TRACK
    assert capsys.readouterr().out.strip() == shlex.join(["meld", str(backup.resolve()), str(track.resolve())])
    assert "MusicBrainz request: GET" in caplog.text
    assert '"title": "Walking on Broken Glass"' in caplog.text
    assert "Using MusicBrainz result already fetched" in caplog.text


def test_saved_result_loads_with_mediascan_schema(track: Path, http_mock: MagicMock) -> None:
    from mediascan.types.track.track_yaml_file_loader import load_track_yaml_file

    assert musicbrainz.run(specific_file=track)
    loaded = load_track_yaml_file(track)
    assert loaded.media_file_data is not None
    data = loaded.media_file_data[0].acoustid_data
    assert data is not None
    recording = data.results[0].recordings[0]
    assert (recording.id, recording.title, recording.length) == (REC_1, "Walking on Broken Glass", 252000)
    assert (recording.video, recording.disambiguation, recording.first_release_date) == (False, "", "1992")
    assert recording.isrcs == ["GBARL9200074", "USBM10255392"]
    live = data.results[0].recordings[1]
    assert (live.video, live.disambiguation, live.first_release_date) == (True, "live", "1992-06-01")


def test_acoustid_then_musicbrainz_loads_with_mediascan_schema(
    tmp_path: Path, http_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mediascan.types.track.track_yaml_file_loader import load_track_yaml_file

    path = tmp_path / "02 - Fresh.yml"
    path.write_text(CORE + "mediaFileData:\n  - fpcalcData: {duration: 252, fingerprint: AQADtE-example}\n")
    response = {"status": "ok", "results": [{"id": "match-1", "score": 0.9, "recordings": [{"id": REC_1}]}]}
    monkeypatch.setattr(acoustid, "urlopen", MagicMock(return_value=HttpResponse(json.dumps(response).encode())))
    assert acoustid.run("key", specific_file=path)
    assert musicbrainz.run(specific_file=path)
    loaded = load_track_yaml_file(path)
    assert loaded.media_file_data is not None
    data = loaded.media_file_data[0].acoustid_data
    assert data is not None
    assert data.results[0].recordings[0].title == "Walking on Broken Glass"


def test_dry_run_writes_proposal_only(track: Path, http_mock: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    assert musicbrainz.run(specific_file=track, dry_run=True)
    assert http_mock.call_count == 2
    assert track.read_text() == TRACK
    assert not track.with_name(track.name + ".bak").exists()
    (proposal,) = track.parent.glob("musicbrainz-proposal-*/01 - Track.yml")
    assert recordings(proposal)[0]["title"] == "Walking on Broken Glass"
    assert capsys.readouterr().out.strip() == shlex.join(["meld", str(track.resolve()), str(proposal)])


def test_recordings_with_isrcs_are_skipped_without_force(
    track: Path, http_mock: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    assert musicbrainz.run(specific_file=track)
    http_mock.reset_mock()
    filled = track.read_text()
    with caplog.at_level(logging.INFO):
        assert musicbrainz.run(specific_file=track)
    # REC_2 has no ISRCs in MusicBrainz, so it is checked again; REC_1 is skipped.
    assert [call.args[0].full_url.split("?")[0].rsplit("/", 1)[-1] for call in http_mock.call_args_list] == [REC_2]
    assert track.read_text() == filled
    assert "2 recording(s) already have ISRCs" in caplog.text


def test_force_looks_up_every_recording(track: Path, http_mock: MagicMock) -> None:
    track.write_text(TRACK.replace("isrcs: []", "isrcs: [OLD]"))
    assert musicbrainz.run(specific_file=track, force=True)
    assert http_mock.call_count == 2
    assert recordings(track)[0]["isrcs"] == ["GBARL9200074", "USBM10255392"]


def test_fully_populated_track_does_not_count(track: Path, http_mock: MagicMock) -> None:
    done = track.with_name("00 - Done.yml")
    done.write_text(
        CORE
        + "mediaFileData:\n  - acoustidData:\n      status: ok\n      results:\n        - id: m\n          score: 1\n          recordings:\n"
        + f"            - {{id: {REC_1}, isrcs: [X]}}\n"
    )
    assert musicbrainz.run(specific_dir=track.parent, count=1)
    assert http_mock.call_count == 2
    assert recordings(track)[0]["title"] == "Walking on Broken Glass"


def test_missing_acoustid_data_warns(tmp_path: Path, http_mock: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "01.yml"
    path.write_text(CORE + "mediaFileData:\n  - path: /music/example.flac\n    acoustidData: {}\n")
    assert musicbrainz.run(specific_file=path)
    http_mock.assert_not_called()
    assert "run acoustid first" in caplog.text


def test_invalid_recording_ids_are_skipped(track: Path, http_mock: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    track.write_text(TRACK.replace(REC_2, "../../evil?x=1"))
    assert musicbrainz.run(specific_file=track)
    assert all(REC_1 in call.args[0].full_url for call in http_mock.call_args_list)
    assert "invalid MusicBrainz recording ID" in caplog.text


def test_not_found_recording_is_left_unchanged(track: Path, http_mock: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    def respond_missing(request: Request, timeout: float) -> HttpResponse:
        if REC_2 in request.full_url:
            raise HTTPError(request.full_url, 404, "Not Found", None, BytesIO(b'{"error": "Not Found"}'))  # type: ignore[arg-type]
        return respond(request, timeout)

    http_mock.side_effect = respond_missing
    assert musicbrainz.run(specific_file=track)
    first, second, _ = recordings(track)
    assert first["title"] == "Walking on Broken Glass"
    assert second == {"id": REC_2}
    assert "not found; leaving unchanged" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        HTTPError("u", 503, "Unavailable", None, BytesIO(b"slow down")),  # type: ignore[arg-type]
        URLError("offline"),
    ],
)
def test_request_errors_fail_without_saving(track: Path, http_mock: MagicMock, error: Exception) -> None:
    http_mock.side_effect = error
    assert not musicbrainz.run(specific_file=track)
    assert track.read_text() == TRACK


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        json.dumps({"id": REC_1, "title": 5}),
        json.dumps({"id": REC_1, "video": "no"}),
        json.dumps({"id": REC_1, "disambiguation": []}),
        json.dumps({"id": REC_1, "first-release-date": 2000}),
        json.dumps({"id": REC_1, "length": True}),
        json.dumps({"id": REC_1, "length": -1}),
        json.dumps({"id": REC_1, "title": "T", "length": "long", "isrcs": []}),
        json.dumps({"id": REC_1, "title": "T", "length": 1, "isrcs": [""]}),
        "not json",
    ],
)
def test_invalid_responses_never_save(track: Path, http_mock: MagicMock, body: str) -> None:
    http_mock.side_effect = None
    http_mock.return_value = HttpResponse(body.encode())
    assert not musicbrainz.run(specific_file=track)
    assert track.read_text() == TRACK


def test_missing_and_null_fields_are_stored_as_null(track: Path, http_mock: MagicMock) -> None:
    http_mock.side_effect = lambda request, timeout: HttpResponse(
        json.dumps({"id": REC_1 if REC_1 in request.full_url else REC_2, "title": "T", "length": None}).encode()
    )
    assert musicbrainz.run(specific_file=track)
    assert recordings(track)[0] == {
        "id": REC_1,
        "title": "T",
        "length": None,
        "video": None,
        "disambiguation": None,
        "firstReleaseDate": None,
        "isrcs": [],
    }


def test_every_field_except_id_is_replaced(track: Path, http_mock: MagicMock) -> None:
    stale = "title: Old, length: 1, video: true, disambiguation: old, firstReleaseDate: '1900', isrcs: [], extra: kept"
    track.write_text(TRACK.replace(f"- id: {REC_2}\n", f"- {{id: {REC_2}, {stale}}}\n"))
    assert musicbrainz.run(specific_file=track)
    assert recordings(track)[1] == {**SAVED_2, "extra": "kept"}


def test_merged_recording_keeps_requested_id(track: Path, http_mock: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    http_mock.side_effect = lambda request, timeout: HttpResponse(json.dumps({**DETAILS[REC_1], "id": REC_2}).encode())
    assert musicbrainz.run(specific_file=track)
    assert [recording["id"] for recording in recordings(track)] == [REC_1, REC_2, REC_1]
    assert "resolved to" in caplog.text


def test_requests_are_rate_limited(
    track: Path, http_mock: MagicMock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(musicbrainz.time, "monotonic", MagicMock(side_effect=[10, 10.25, 12]))
    with caplog.at_level(logging.INFO):
        assert musicbrainz.run(specific_file=track, sleep=2)
    assert musicbrainz.time.sleep.call_args.args[0] == pytest.approx(1.75)
    assert "Sleeping 1.750 second(s) before next MusicBrainz request" in caplog.text


def test_default_sleep_meets_musicbrainz_limit() -> None:
    assert musicbrainz.MIN_SLEEP >= 1


def test_offline_prints_unique_urls_without_requests(
    track: Path, http_mock: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert musicbrainz.run(specific_file=track, dry_run=True, offline=True)
    http_mock.assert_not_called()
    assert track.read_text() == TRACK
    assert capsys.readouterr().out.splitlines() == [
        f"{musicbrainz.RECORDING_URL}{REC_1}?inc=isrcs&fmt=json",
        f"{musicbrainz.RECORDING_URL}{REC_2}?inc=isrcs&fmt=json",
    ]


@pytest.mark.parametrize("kwargs", [{"offline": True}, {"sleep": 0.5}, {"count": -1}])
def test_run_rejects_invalid_options(track: Path, http_mock: MagicMock, kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        musicbrainz.run(specific_file=track, **kwargs)  # type: ignore[arg-type]
    http_mock.assert_not_called()


def test_changed_source_is_not_overwritten(track: Path, http_mock: MagicMock) -> None:
    def edit_then_respond(request: Request, timeout: float) -> HttpResponse:
        track.write_text(TRACK + "# concurrently edited\n")
        return respond(request, timeout)

    http_mock.side_effect = edit_then_respond
    assert not musicbrainz.run(specific_file=track)
    assert track.read_text() == TRACK + "# concurrently edited\n"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--count", "-1"],
        ["--file", "a.yml", "--dir", "directory"],
        ["--sleep", "0.5"],
        ["--sleep", "nan"],
        ["--offline"],
    ],
)
def test_invalid_cli_arguments(monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["musicbrainz", *arguments])
    with pytest.raises(SystemExit) as error:
        musicbrainz.main()
    assert error.value.code == 2


def test_cli_dry_run_offline(track: Path, monkeypatch: pytest.MonkeyPatch, http_mock: MagicMock) -> None:
    monkeypatch.setattr(sys, "argv", ["musicbrainz", "--file", str(track), "-n", "--offline", "-c", "1", "--force"])
    assert musicbrainz.main() == 0
    http_mock.assert_not_called()


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_cli_passes_options(track: Path, monkeypatch: pytest.MonkeyPatch, overwrite: bool, reverse: bool) -> None:
    run = MagicMock(return_value=True)
    monkeypatch.setattr(musicbrainz, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["musicbrainz", "--file", str(track), "-n", "--force", "--sleep", "2",
         *(["--overwrite"] if overwrite else []), *(["--reverse"] if reverse else [])],
    )
    assert musicbrainz.main() == 0
    assert run.call_args.args == (track, None, None, True, True, 2.0, False, overwrite, reverse)


@pytest.mark.parametrize("collection", [False, True])
@pytest.mark.parametrize("count", [None, 1])
def test_reverse_traverses_files_in_exact_opposite_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, http_mock: MagicMock,
    capsys: pytest.CaptureFixture[str], collection: bool, count: int | None,
) -> None:
    root = tmp_path / "data"
    for index, name in enumerate(["z-album/02.yml", "z-album/01.yaml", "a-album/01.yml"]):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        recording_id = f"00000000-0000-0000-0000-{index:012d}"
        path.write_text(TRACK.replace(REC_1, recording_id).replace(REC_2, recording_id))
    # Ineligible files must not consume --count, even when encountered first.
    (root / "z-album" / "99.yml").write_text(CORE)
    (root / "artist.yml").write_text(TRACK)
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    specific_dir = None if collection else root
    assert musicbrainz.run(specific_dir=specific_dir, dry_run=True, offline=True)
    normal = capsys.readouterr().out.splitlines()
    assert len(normal) == 3
    originals = {path: path.read_text() for path in root.rglob("*") if path.is_file()}
    assert musicbrainz.run(specific_dir=specific_dir, dry_run=True, offline=True, reverse=True, count=count)
    expected = list(reversed(normal))
    assert capsys.readouterr().out.splitlines() == expected[:count]
    assert all(path.read_text() == content for path, content in originals.items())
    http_mock.assert_not_called()


def test_reverse_single_file_preserves_recording_order(
    track: Path, http_mock: MagicMock, capsys: pytest.CaptureFixture[str],
) -> None:
    assert musicbrainz.run(specific_file=track, dry_run=True, offline=True, reverse=True)
    assert capsys.readouterr().out.splitlines() == [
        musicbrainz.build_request(REC_1).full_url,
        musicbrainz.build_request(REC_2).full_url,
    ]
    http_mock.assert_not_called()


def test_reverse_count_zero_does_not_enumerate_files(monkeypatch: pytest.MonkeyPatch) -> None:
    def paths(specific_file: Path | None, specific_dir: Path | None):
        raise AssertionError("count=0 must not enumerate files")
        yield Path("unused")

    monkeypatch.setattr(musicbrainz, "track_files", paths)
    assert musicbrainz.run(count=0, reverse=True)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("overwrite", [False, True])
def test_overwrite_unchanged_musicbrainz_results(
    track: Path,
    http_mock: MagicMock,
    dry_run: bool,
    overwrite: bool,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    data = YAML(typ="safe").load(TRACK)
    for result in data["mediaFileData"][0]["acoustidData"]["results"]:
        for recording in result["recordings"]:
            recording.update(SAVED_1 if recording["id"] == REC_1 else SAVED_2)
    original = json.dumps(data)
    track.write_text(original)
    with caplog.at_level(logging.INFO):
        assert musicbrainz.run(specific_file=track, force=True, overwrite=overwrite, dry_run=dry_run)
    assert http_mock.call_count == 2
    backup = track.with_name(track.name + ".bak")
    if overwrite:
        if dry_run:
            (output,) = track.parent.glob("musicbrainz-proposal-*/" + track.name)
            assert track.read_text() == original
            assert not backup.exists()
        else:
            output = track
            assert backup.read_text() == original
        text = output.read_text()
        assert '"GBARL9200074"' not in text
        assert YAML(typ="safe").load(text) == data
        assert "&id" not in text and "*id" not in text
        assert capsys.readouterr().out.startswith("meld ")
        assert "--overwrite requested despite unchanged recording details" in caplog.text
        assert "Diff command: meld " in caplog.text
    else:
        assert track.read_text() == original
        assert not backup.exists()
        assert not list(track.parent.glob("musicbrainz-proposal-*"))
        assert capsys.readouterr().out == ""
        assert "No YAML write and no meld command" in caplog.text


def test_interrupted_track_logs_pending_updates_without_saving(
    track: Path, http_mock: MagicMock, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt_second(request: Request, timeout: float) -> HttpResponse:
        if REC_2 in request.full_url:
            raise KeyboardInterrupt
        return respond(request, timeout)

    http_mock.side_effect = interrupt_second
    with caplog.at_level(logging.INFO), pytest.raises(KeyboardInterrupt):
        musicbrainz.run(specific_file=track, force=True, overwrite=True)
    assert track.read_text() == TRACK
    assert not track.with_name(track.name + ".bak").exists()
    assert capsys.readouterr().out == ""
    assert "Recording 1/3:" in caplog.text
    assert "Recording 2/3:" in caplog.text
    assert "staged changes to" in caplog.text
    assert "not yet written to YAML" in caplog.text
    assert "Interrupted while processing this track" in caplog.text
    assert "No automatic save of pending updates on interruption" in caplog.text


@pytest.mark.parametrize("mode", ["skipped", "offline", "failed", "not_found"])
def test_overwrite_requires_successful_musicbrainz_lookup(track: Path, http_mock: MagicMock, mode: str) -> None:
    data = YAML(typ="safe").load(TRACK)
    for result in data["mediaFileData"][0]["acoustidData"]["results"]:
        for recording in result["recordings"]:
            recording.update(SAVED_1)
    original = json.dumps(data)
    track.write_text(original)
    if mode == "failed":
        http_mock.side_effect = URLError("unavailable")
    elif mode == "not_found":

        def not_found(request: Request, timeout: float) -> HttpResponse:
            raise HTTPError(request.full_url, 404, "Not Found", None, BytesIO(b"{}"))  # type: ignore[arg-type]

        http_mock.side_effect = not_found
    assert musicbrainz.run(
        specific_file=track, force=mode != "skipped", overwrite=True, dry_run=mode == "offline", offline=mode == "offline"
    ) == (mode != "failed")
    assert track.read_text() == original
    assert not track.with_name(track.name + ".bak").exists()
    assert not list(track.parent.glob("musicbrainz-proposal-*"))


def test_extra_sleep_grows_on_503_and_shrinks_on_2xx(caplog: pytest.LogCaptureFixture) -> None:
    extra = musicbrainz.ExtraSleep()
    with caplog.at_level(logging.INFO):
        extra.record(200)
        assert extra.seconds == 0
        extra.record(503)
        extra.record(503)
        assert extra.seconds == 1.0
        extra.record(404)
        extra.record(500)
        assert extra.seconds == 1.0
        extra.record(200)
        assert extra.seconds == 0.9
        for _ in range(20):
            extra.record(204)
        assert extra.seconds == 0
    assert "temporary extra sleep increased from 0.500 to 1.000" in caplog.text
    assert "temporary extra sleep decreased from 1.000 to 0.900" in caplog.text
    assert caplog.text.count("temporary extra sleep") == 12


def test_503_increases_sleep_for_later_requests(
    tmp_path: Path, http_mock: MagicMock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids = [REC_1, REC_2, "11111111-2222-3333-4444-555555555555", "66666666-7777-8888-9999-000000000000"]
    for index, recording_id in enumerate(ids):
        (tmp_path / f"{index:02}.yml").write_text(
            CORE
            + "mediaFileData:\n  - acoustidData:\n      status: ok\n      results:\n        - id: m\n          score: 1\n"
            + f"          recordings: [{{id: {recording_id}}}]\n"
        )
    statuses = iter([503, 503, 200, 200, 200, 200])

    def respond_status(request: Request, timeout: float) -> HttpResponse:
        status = next(statuses)
        if status == 503:
            raise HTTPError(request.full_url, 503, "Service Unavailable", None, BytesIO(b"busy"))  # type: ignore[arg-type]
        recording_id = urlsplit(request.full_url).path.rsplit("/", 1)[-1]
        return HttpResponse(json.dumps({"id": recording_id, "title": "T", "length": 1, "isrcs": []}).encode())

    http_mock.side_effect = respond_status
    monkeypatch.setattr(musicbrainz.time, "monotonic", MagicMock(return_value=0))
    with caplog.at_level(logging.INFO):
        assert musicbrainz.run(specific_dir=tmp_path)
    assert http_mock.call_count == 6
    delays = [call.args[0] for call in musicbrainz.time.sleep.call_args_list]
    base = musicbrainz.MIN_SLEEP
    assert delays == pytest.approx([base + 0.5, base + 1.0, base + 0.9, base + 0.8, base + 0.7])
    assert "(temporary extra sleep 1.000)" in caplog.text
    assert (
        YAML(typ="safe").load((tmp_path / "03.yml").read_text())["mediaFileData"][0]["acoustidData"]["results"][0]["recordings"][
            0
        ]["title"]
        == "T"
    )


def test_acoustid_output_loads_before_musicbrainz(track: Path) -> None:
    from mediascan.types.track.track_yaml_file_loader import load_track_yaml_file

    loaded = load_track_yaml_file(track)
    assert loaded.media_file_data is not None
    data = loaded.media_file_data[0].acoustid_data
    assert data is not None
    recording = data.results[0].recordings[1]
    assert (recording.id, recording.title, recording.isrcs) == (REC_2, None, None)


def test_failed_lookup_is_retried_until_success(track: Path, http_mock: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
    failures = iter([URLError("down"), HTTPError("u", 500, "Error", None, BytesIO(b"oops")), None, None, None])  # type: ignore[arg-type]

    def flaky(request: Request, timeout: float) -> HttpResponse:
        error = next(failures, None)
        if error is not None:
            raise error
        return respond(request, timeout)

    http_mock.side_effect = flaky
    assert musicbrainz.run(specific_file=track)
    assert http_mock.call_count == 4
    assert recordings(track)[:2] == [SAVED_1, SAVED_2]
    assert "failed (attempt 1 of 5)" in caplog.text
    assert "failed (attempt 2 of 5)" in caplog.text


def test_gives_up_after_five_attempts_and_moves_on(
    tmp_path: Path, track: Path, http_mock: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    def rec_1_down(request: Request, timeout: float) -> HttpResponse:
        if REC_1 in request.full_url:
            raise HTTPError(request.full_url, 503, "Unavailable", None, BytesIO(b"busy"))  # type: ignore[arg-type]
        return respond(request, timeout)

    http_mock.side_effect = rec_1_down
    later = tmp_path / "02 - Later.yml"
    later.write_text(TRACK)
    assert not musicbrainz.run(specific_dir=tmp_path)
    urls = [call.args[0].full_url for call in http_mock.call_args_list]
    assert sum(REC_1 in url for url in urls) == musicbrainz.MAX_ATTEMPTS == 5
    assert sum(REC_2 in url for url in urls) == 1
    assert REC_2 in urls[5]
    for path in (track, later):
        first, second, third = recordings(path)
        assert first == {"id": REC_1, "isrcs": []}
        assert second == SAVED_2
        assert third == {"id": REC_1}
    assert "Giving up on MusicBrainz recording " + REC_1 + " after 5 failed attempts" in caplog.text
    assert "already failed 5 times this run" in caplog.text


def test_not_found_is_not_retried(track: Path, http_mock: MagicMock) -> None:
    def not_found(request: Request, timeout: float) -> HttpResponse:
        raise HTTPError(request.full_url, 404, "Not Found", None, BytesIO(b"{}"))  # type: ignore[arg-type]

    http_mock.side_effect = not_found
    assert musicbrainz.run(specific_file=track)
    assert http_mock.call_count == 2
    assert track.read_text() == TRACK
