import logging
import shlex
import sys
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from mediascan.types.track.track_yaml_file_loader import load_track_yaml_file
from openai import APIConnectionError, APIStatusError, OpenAI

from mediascripts.track.track_yaml import track_yaml_auto_populate as populate

CORE = "trackData:\n  title: Track\n  artist: Artist\n  albumartist: Artist\n  album: Album\n  genre: Rock\n  year: 1995\n"
COMPLETE = CORE + "trackIdData:\n  - isrc: USABC9500001\n    spotifyId: example-track\n"


@pytest.fixture(autouse=True)
def isolate_proposal_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(populate.tempfile, "tempdir", str(tmp_path))


def make_client(content: str | None) -> MagicMock:
    client = MagicMock(spec=OpenAI)
    client.chat.completions.create.return_value.choices[0].message.content = content
    return client


@pytest.fixture
def track(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "Music" / "Artist" / "Album [1995]" / "01 - Track.yml"
    path.parent.mkdir(parents=True)
    path.write_text(CORE, encoding="utf-8")
    return path


@pytest.mark.parametrize("field", sorted(populate.ID_FIELDS))
def test_all_track_identifier_fields_round_trip(field: str) -> None:
    document = populate.validate_track_yaml_content(CORE + f"trackIdData:\n  - {field}: example\n")
    assert populate.has_identifiers(document)


FINGERPRINT_IDS = (
    "trackIdData:\n"
    "  - fpcalcData:\n"
    "      duration: 123.5\n"
    "      fingerprint: AQAA-example-fingerprint\n"
)


def test_fingerprint_data_round_trips_but_is_not_a_streaming_identifier() -> None:
    document = populate.validate_track_yaml_content(CORE + FINGERPRINT_IDS)
    assert not populate.has_identifiers(document)
    assert document["trackIdData"] == [
        {"fpcalcData": {"duration": 123.5, "fingerprint": "AQAA-example-fingerprint"}}
    ]


def test_acoustid_identifier_lists_are_valid_and_count_as_identifiers() -> None:
    content = CORE + FINGERPRINT_IDS + "    acoustidIds: [match-1]\n    musicbrainzRecordingIds: [recording-1]\n"
    document = populate.validate_track_yaml_content(content)
    assert populate.has_identifiers(document)


def test_full_acoustid_response_is_accepted_and_preserved() -> None:
    content = (
        CORE + FINGERPRINT_IDS
        + "    acoustidData:\n"
        + "      status: ok\n"
        + "      results:\n"
        + "        - id: match-1\n"
        + "          score: 0.9\n"
        + "          recordings: [{id: recording-1, title: Track, length: 203000, isrcs: [USSM18500120]}]\n"
    )
    document = populate.validate_track_yaml_content(content)
    completed = populate.validate_track_yaml_content(content + "    spotifyId: example\n")
    populate.preserve_existing_values(document, completed)
    assert "acoustidData" not in populate.ID_FIELDS
    modified = populate.validate_track_yaml_content(content.replace("score: 0.9", "score: 0.1"))
    with pytest.raises(ValueError, match="changed existing track identifiers"):
        populate.preserve_existing_values(document, modified)


@pytest.mark.parametrize("value", ["null", "123", "match-1", "[123]", "['']"])
def test_acoustid_identifier_lists_reject_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="acoustidIds"):
        populate.validate_track_yaml_content(CORE + f"trackIdData:\n  - acoustidIds: {value}\n")


def test_existing_fingerprint_is_preserved_when_adding_service_id(track: Path) -> None:
    original = CORE + FINGERPRINT_IDS
    track.write_text(original)
    response = original + "    deezerId: '12345'\n"
    assert populate.process_track_yaml_file(track, "", 1, 1, client=cast(OpenAI, make_client(response)))
    loaded = load_track_yaml_file(track)
    assert loaded.track_id_data is not None
    fingerprint = loaded.track_id_data[0].fpcalc_data
    assert fingerprint is not None
    assert fingerprint.duration == 123.5
    assert fingerprint.fingerprint == "AQAA-example-fingerprint"
    assert track.with_name(track.name + ".bak").read_text() == original


@pytest.mark.parametrize("original", [CORE, CORE + FINGERPRINT_IDS])
def test_model_cannot_add_or_change_fingerprint(track: Path, original: str) -> None:
    track.write_text(original)
    response = (CORE + FINGERPRINT_IDS).replace("AQAA-example-fingerprint", "invented")
    assert not populate.process_track_yaml_file(track, "", 1, 1, client=cast(OpenAI, make_client(response)))
    assert track.read_text() == original
    assert not track.with_name(track.name + ".bak").exists()


@pytest.mark.parametrize("value", [
    "'not a mapping'", "{unknown: value}", "{duration: true}",
    "{duration: '123'}", "{fingerprint: 123}",
])
def test_invalid_fingerprint_shape_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="fpcalcData"):
        populate.validate_track_yaml_content(CORE + f"trackIdData:\n  - fpcalcData: {value}\n")


@pytest.mark.parametrize(
    "identifiers", ["", "trackIdData: null\n", "trackIdData: []\n", "trackIdData:\n  - spotifyId: null\n    isrc: '  '\n"]
)
def test_missing_identifiers_are_not_complete(identifiers: str) -> None:
    assert not populate.has_identifiers(populate.validate_track_yaml_content(CORE + identifiers))


@pytest.mark.parametrize(
    "content",
    [
        "",
        "[]",
        "null",
        "```yaml\n" + COMPLETE + "```",
        "trackData: [\n",
        CORE + "trackIdData:\n  isrc: example\n",
        CORE + "trackIdData:\n  - unknownId: example\n",
        CORE + "trackIdData:\n  - spotifyUrl: 123\n",
        CORE + "trackIdData:\n  - example\n",
        CORE + "format: mp3\n",
        CORE.replace("year: 1995", "year: true"),
        CORE.replace("year: 1995", "year: '1995'"),
        CORE.replace("  title: Track\n", ""),
        CORE.replace("title: Track", "title: ''"),
        CORE + "trackData: {}\n",
    ],
)
def test_invalid_responses_never_touch_source(track: Path, content: str) -> None:
    client = make_client(content)
    assert not populate.process_track_yaml_file(track, "", 1, 1, client=cast(OpenAI, client))
    assert track.read_text() == CORE
    assert not track.with_name(track.name + ".bak").exists()
    assert not list(track.parent.glob(".*.tmp"))


def test_changed_response_is_validated_backed_up_and_saved_atomically(track: Path) -> None:
    track.chmod(0o640)
    client = make_client(COMPLETE)
    assert populate.process_track_yaml_file(track, COMPLETE, 1, 1, client=cast(OpenAI, client))
    assert track.read_text() == COMPLETE
    assert track.with_name(track.name + ".bak").read_text() == CORE
    assert track.stat().st_mode & 0o777 == 0o640
    loaded = load_track_yaml_file(track)
    assert loaded.track_id_data is not None
    assert loaded.track_id_data[0].spotify_id == "example-track"
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert COMPLETE in messages[1]["content"]
    assert str(track) in messages[1]["content"]
    assert not list(track.parent.glob(".*.tmp"))


@pytest.mark.parametrize("field", ["appleMusicId", "deezerId", "tidalId", "spotifyId"])
@pytest.mark.parametrize("digits", ["123456789", "00123456789"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_model_numeric_service_ids_are_quoted_without_losing_digits(
    track: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    digits: str,
    dry_run: bool,
) -> None:
    response = CORE + f"trackIdData:\n  - {field}: {digits}\n"
    assert populate.process_track_yaml_file(
        track,
        "",
        1,
        1,
        client=cast(OpenAI, make_client(response)),
        dry_run=dry_run,
    )
    assert f"LLM Result = {digits!r}" in capsys.readouterr().out
    if dry_run:
        assert track.read_text() == CORE
        assert not track.with_name(track.name + ".bak").exists()
    else:
        document = populate.validate_track_yaml_content(track.read_text())
        assert document["trackIdData"] == [{field: digits}]
        assert track.with_name(track.name + ".bak").read_text() == CORE


@pytest.mark.parametrize("value", ["true", "12.5", "[123]", "{id: 123}", "-123"])
def test_invalid_identifier_types_are_reported_precisely(
    track: Path,
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    response = CORE + f"trackIdData:\n  - deezerId: {value}\n"
    assert not populate.process_track_yaml_file(
        track,
        "",
        1,
        1,
        client=cast(OpenAI, make_client(response)),
        dry_run=True,
    )
    assert "trackIdData[0].deezerId must be a string or null; got" in caplog.text
    assert track.read_text() == CORE


def test_empty_sidecar_can_be_filled(track: Path) -> None:
    track.write_text("")
    assert populate.process_track_yaml_file(track, "", 1, 1, client=cast(OpenAI, make_client(COMPLETE)))
    assert track.read_text() == COMPLETE


@pytest.mark.parametrize("dry_run", [False, True])
def test_meld_command_compares_original_and_completed_yaml(
    track: Path,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    track = track.rename(track.with_name("Song's $(not-a-command).yml"))
    assert populate.process_track_yaml_file(
        track,
        "",
        1,
        1,
        client=cast(OpenAI, make_client(COMPLETE)),
        dry_run=dry_run,
    )
    output = capsys.readouterr().out
    command = next(line for line in output.splitlines() if line.startswith("meld "))
    executable, before, after = shlex.split(command)
    assert executable == "meld"
    assert Path(before).read_text() == CORE
    assert Path(after).read_text() == COMPLETE
    if dry_run:
        assert Path(before) == track
        assert Path(after).parent != track.parent
        assert Path(after).stat().st_mode & 0o777 == 0o600
        assert not track.with_name(track.name + ".bak").exists()
    else:
        assert Path(before) == track.with_name(track.name + ".bak")
        assert Path(after) == track


def test_diff_shows_added_identifiers_before_source_is_replaced(
    track: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    replace = populate.os.replace

    def check_diff_then_replace(source: Path, destination: Path) -> None:
        output = capsys.readouterr().out
        assert str(track) in output
        assert "trackIdData[0].isrc: Track YAML = (missing); LLM Result = 'USABC9500001'" in output
        assert "trackIdData[0].spotifyId: Track YAML = (missing); LLM Result = 'example-track'" in output
        assert "trackData.title:" not in output
        assert track.read_text() == CORE
        replace(source, destination)

    monkeypatch.setattr(populate.os, "replace", check_diff_then_replace)
    assert populate.process_track_yaml_file(track, "", 1, 1, client=cast(OpenAI, make_client(COMPLETE)))


def test_diff_shows_filled_core_fields_and_multiple_identifier_entries(
    track: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = CORE.replace("title: Track", "title: null") + "trackIdData:\n  - isrc: USABC9500001\n"
    completed = COMPLETE + "  - deezerId: other-recording\n"
    track.write_text(original)
    assert populate.process_track_yaml_file(track, "", 1, 1, client=cast(OpenAI, make_client(completed)))
    output = capsys.readouterr().out
    assert "trackData.title: Track YAML = None; LLM Result = 'Track'" in output
    assert "trackIdData[1].deezerId: Track YAML = (missing); LLM Result = 'other-recording'" in output
    assert "trackIdData[0].isrc:" not in output


def test_no_diff_for_unchanged_response(track: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert populate.process_track_yaml_file(track, "", 1, 1, client=cast(OpenAI, make_client(CORE)))
    assert capsys.readouterr().out == ""


def test_no_diff_for_rejected_response(track: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert not populate.process_track_yaml_file(
        track,
        "",
        1,
        1,
        client=cast(OpenAI, make_client(COMPLETE.replace("title: Track", "title: Wrong"))),
    )
    assert capsys.readouterr().out == ""


def test_formatting_only_response_reports_no_metadata_differences(
    track: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        assert populate.process_track_yaml_file(
            track,
            "",
            1,
            1,
            client=cast(OpenAI, make_client(CORE.replace("title: Track", "title: 'Track'"))),
        )
    output = capsys.readouterr().out
    assert "-  title: Track\n+  title: 'Track'\n" in output
    assert any(line.startswith("meld ") for line in output.splitlines())
    assert "No metadata differences (YAML formatting only)" in caplog.text


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_unified_file_diff_shows_proposed_yaml(
    track: Path, capsys: pytest.CaptureFixture[str], dry_run: bool, empty: bool,
) -> None:
    if empty:
        track.write_text("")
    assert populate.process_track_yaml_file(
        track, "", 1, 1, client=cast(OpenAI, make_client(COMPLETE)), dry_run=dry_run,
    )
    output = capsys.readouterr().out
    assert f"--- {track}\n+++ {track} (proposed)\n@@ " in output
    assert "+trackIdData:\n+  - isrc: USABC9500001\n+    spotifyId: example-track\n" in output
    if empty:
        assert "+trackData:\n" in output
    else:
        assert "   year: 1995\n" in output
    if dry_run:
        assert track.read_text() == ("" if empty else CORE)


def test_unchanged_response_leaves_source_and_backup_untouched(track: Path) -> None:
    track.write_text(COMPLETE)
    backup = track.with_name(track.name + ".bak")
    backup.write_text("previous backup")
    before = track.stat().st_mtime_ns
    assert populate.process_track_yaml_file(track, "", 1, 1, client=cast(OpenAI, make_client(COMPLETE)))
    assert track.stat().st_mtime_ns == before
    assert backup.read_text() == "previous backup"


@pytest.mark.parametrize("content", [None, "", CORE.replace("title: Track", "title: Other")])
def test_empty_or_changed_metadata_responses_are_rejected(track: Path, content: str | None) -> None:
    assert not populate.process_track_yaml_file(track, "", 1, 1, client=cast(OpenAI, make_client(content)))
    assert track.read_text() == CORE


def test_existing_identifiers_cannot_be_changed(track: Path) -> None:
    track.write_text(COMPLETE)
    client = make_client(COMPLETE.replace("USABC9500001", "different"))
    assert not populate.process_track_yaml_file(track, "", 1, 1, client=cast(OpenAI, client))
    assert track.read_text() == COMPLETE


@pytest.mark.parametrize("value", ["", " ", None])
def test_blank_metadata_can_be_filled(value: str | None) -> None:
    populate.preserve_existing_values({"trackData": {"title": value}}, {"trackData": {"title": "Track"}})
    populate.preserve_existing_values({"trackIdData": []}, {})


@pytest.mark.parametrize("status", [410, 429, 500, None])
def test_provider_failures_leave_source_unchanged(track: Path, caplog: pytest.LogCaptureFixture, status: int | None) -> None:
    client = make_client(COMPLETE)
    request = MagicMock()
    response = MagicMock(status_code=status, request=request)
    client.chat.completions.create.side_effect = (
        APIStatusError("failure", response=response, body=None) if status is not None else APIConnectionError(request=request)
    )
    with caplog.at_level(logging.ERROR):
        assert not populate.process_track_yaml_file(track, "", 1, 1, client=cast(OpenAI, client))
    assert caplog.text
    assert track.read_text() == CORE


def test_atomic_replace_failure_keeps_original_and_cleans_temp(track: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(populate.os, "replace", fail_replace)
    assert not populate.process_track_yaml_file(track, "", 1, 1, client=cast(OpenAI, make_client(COMPLETE)))
    assert track.read_text() == CORE
    assert track.with_name(track.name + ".bak").read_text() == CORE
    assert not list(track.parent.glob(".*.tmp"))


def test_discovery_follows_library_symlinks_and_excludes_non_tracks(tmp_path: Path) -> None:
    library = tmp_path / "library"
    album = library / "Artist" / "Album [1995]"
    album.mkdir(parents=True)
    for filename in ["artist.yml", "album.yml", "files.yml", "01 - Track.yml", "02 - Track.yaml", "01 - Track.yml.bak"]:
        (album / filename).touch()
    (library / "Artist" / "artist.yml").touch()
    data = tmp_path / "data"
    data.mkdir()
    (data / "Music").symlink_to(library, target_is_directory=True)
    paths = populate.load_input_files(data)
    assert [path.name for path in paths] == ["01 - Track.yml", "02 - Track.yaml"]
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "song.yml").touch()
    assert populate.load_input_files(loose) == []
    assert populate.load_input_files(loose, album_directories_only=False) == [loose / "song.yml"]


@pytest.mark.parametrize("force", [False, True])
def test_collection_skip_force_reference_examples_and_pacing(
    track: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    complete = track.with_name("02 - Complete.yml")
    complete.write_text(COMPLETE)
    third = track.with_name("03 - Missing.yml")
    third.write_text(CORE)
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(track.parents[4]))
    client = make_client(COMPLETE)
    monkeypatch.setattr(populate, "get_client", lambda: cast(OpenAI, client))

    def keep_order(paths: list[Path]) -> None:
        return None

    monkeypatch.setattr(populate.random, "shuffle", keep_order)
    sleeps: list[float] = []
    monkeypatch.setattr(populate.time, "sleep", sleeps.append)
    assert populate.main_loop(7, force=force)
    assert client.chat.completions.create.call_count == (3 if force else 2)
    assert sleeps == [7] * (2 if force else 1)
    assert COMPLETE in client.chat.completions.create.call_args.kwargs["messages"][1]["content"]


@pytest.mark.parametrize("llm_enabled", [False, True])
def test_no_client_for_disabled_llm_or_complete_file(track: Path, monkeypatch: pytest.MonkeyPatch, llm_enabled: bool) -> None:
    if llm_enabled:
        track.write_text(COMPLETE)
    monkeypatch.delenv("MOONGAS_COLLECTION_ROOTDIR", raising=False)

    def unexpected_client() -> OpenAI:
        pytest.fail("Client must not be created")

    monkeypatch.setattr(populate, "get_client", unexpected_client)
    before = track.read_bytes()
    assert populate.main_loop(0, llm_enabled=llm_enabled, specific_file=track)
    assert track.read_bytes() == before


def test_clean_removes_only_track_yaml_backups(track: Path) -> None:
    names = ["01 - Track.yml.bak", "02 - Track.yaml.bak", "artist.yml.bak", "album.yml.bak", "song.mp3.bak"]
    for name in names:
        track.with_name(name).touch()
    populate.clean(specific_dir=track.parent)
    for name in names:
        assert track.with_name(name).exists() is (name not in names[:2])


def test_clean_follows_library_symlinks(track: Path, tmp_path: Path) -> None:
    backup = track.with_name(track.name + ".bak")
    backup.touch()
    root = tmp_path / "collection"
    data = root / "data"
    data.mkdir(parents=True)
    (data / "Music").symlink_to(track.parents[2], target_is_directory=True)
    populate.clean(specific_dir=data)
    assert not backup.exists()
    assert track.exists()


def test_failed_file_does_not_stop_batch(track: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = track.with_name("02 - Track.yml")
    other.write_text(CORE)
    client = make_client("invalid")
    monkeypatch.setattr(populate, "get_client", lambda: cast(OpenAI, client))
    assert not populate.main_loop(0, specific_dir=track.parent)
    assert client.chat.completions.create.call_count == 2


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--file", "/nonexistent/track.yml"],
        ["--dir", "/nonexistent"],
    ],
)
def test_cli_reports_invalid_paths(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("MOONGAS_COLLECTION_ROOTDIR", raising=False)
    monkeypatch.setattr(sys, "argv", ["track-yaml-auto-populate", *arguments])
    assert populate.main() == 1
    assert caplog.text


def test_cli_llm_disabled_and_single_file_clean(track: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["track-yaml-auto-populate", "--file", str(track), "--llm=false"])
    assert populate.main() == 0
    backup = track.with_name(track.name + ".bak")
    backup.touch()
    other = track.with_name("other.yml.bak")
    other.touch()
    monkeypatch.setattr(sys, "argv", ["track-yaml-auto-populate", "--file", str(track), "--clean"])
    assert populate.main() == 0
    assert not backup.exists()
    assert other.exists()


@pytest.mark.parametrize("flag", ["-n", "--dry-run"])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("scope", ["--file", "--dir"])
def test_cli_dry_run_requests_updates_and_shows_diff_without_writing(
    track: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    flag: str,
    force: bool,
    scope: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    complete = track.with_name("02 - Complete.yml")
    complete.write_text(COMPLETE)
    backup = track.with_name(track.name + ".bak")
    backup.write_text("previous backup")
    before = {path: path.read_bytes() for path in track.parent.iterdir()}
    client = make_client(COMPLETE)
    factory = MagicMock(return_value=client)
    sleep = MagicMock()
    save = MagicMock()
    monkeypatch.setattr(populate, "get_client", factory)
    monkeypatch.setattr(populate, "save_yaml_atomically", save)
    monkeypatch.setattr(populate.time, "sleep", sleep)
    monkeypatch.setattr(
        sys,
        "argv",
        ["track-yaml-auto-populate", scope, str(track if scope == "--file" else track.parent), flag]
        + (["--force"] if force else []),
    )
    with caplog.at_level(logging.INFO):
        assert populate.main() == 0
    factory.assert_called_once()
    expected = 2 if scope == "--dir" and force else 1
    assert client.chat.completions.create.call_count == expected
    assert sleep.call_count == expected - 1
    save.assert_not_called()
    assert "[dry-run] Diff shown" in caplog.text
    output = capsys.readouterr().out
    assert str(track) in output
    assert "trackIdData[0].spotifyId: Track YAML = (missing); LLM Result = 'example-track'" in output
    if scope == "--dir":
        assert ("Skipping already-complete" in caplog.text) is not force
    assert {path: path.read_bytes() for path in track.parent.iterdir()} == before


@pytest.mark.parametrize("scope", ["--file", "--dir"])
def test_cli_dry_run_clean_preserves_backups(
    track: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scope: str,
) -> None:
    backup = track.with_name(track.name + ".bak")
    backup.write_text("backup")
    artist_backup = track.with_name("artist.yml.bak")
    artist_backup.write_text("artist backup")
    monkeypatch.setattr(
        sys,
        "argv",
        ["track-yaml-auto-populate", scope, str(track if scope == "--file" else track.parent), "--clean", "--dry-run"],
    )
    with caplog.at_level(logging.INFO):
        assert populate.main() == 0
    assert f"[dry-run] Would remove backup file: {backup}" in caplog.text
    assert str(artist_backup) not in caplog.text
    assert backup.read_text() == "backup"
    assert artist_backup.read_text() == "artist backup"
    assert track.read_text() == CORE


def test_dry_run_respects_disabled_llm(
    track: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["track-yaml-auto-populate", "--file", str(track), "--dry-run", "--llm=false"],
    )
    with caplog.at_level(logging.INFO):
        assert populate.main() == 0
    assert "LLM lookup disabled" in caplog.text
    assert "Would request" not in caplog.text
    assert track.read_text() == CORE


@pytest.mark.parametrize("flag,count,expected", [("-c", "1", 1), ("--count", "2", 2), ("--count", "0", 0), ("-c", "10", 3)])
@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_count_caps_eligible_attempts(
    track: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    flag: str,
    count: str,
    expected: int,
    dry_run: bool,
) -> None:
    track.with_name("00 - Complete.yml").write_text(COMPLETE)
    for index in (2, 3):
        track.with_name(f"0{index} - Track.yml").write_text(CORE)
    before = {path: path.read_bytes() for path in track.parent.iterdir()}
    client = make_client("invalid")
    factory = MagicMock(return_value=client)
    sleep = MagicMock()
    monkeypatch.setattr(populate, "get_client", factory)
    monkeypatch.setattr(populate.time, "sleep", sleep)
    monkeypatch.setattr(
        sys,
        "argv",
        ["track-yaml-auto-populate", "--dir", str(track.parent), flag, count] + (["--dry-run"] if dry_run else []),
    )
    with caplog.at_level(logging.INFO):
        assert populate.main() == (0 if expected == 0 else 1)
    assert client.chat.completions.create.call_count == expected
    assert sleep.call_count == max(0, expected - 1)
    assert factory.call_count == (1 if expected else 0)
    assert "Skipping already-complete" in caplog.text
    assert {path: path.read_bytes() for path in track.parent.iterdir()} == before


def test_count_zero_applies_to_single_forced_file(track: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    track.write_text(COMPLETE)
    factory = MagicMock()
    monkeypatch.setattr(populate, "get_client", factory)
    assert populate.main_loop(0, specific_file=track, force=True, count=0)
    factory.assert_not_called()
    assert track.read_text() == COMPLETE


@pytest.mark.parametrize(
    "arguments",
    [
        ["--sleep", "-1"],
        ["--file", "x", "--dir", "y"],
        ["--count", "-1"],
        ["-c", "invalid"],
        ["--count", "1.5"],
        ["-c"],
    ],
)
def test_invalid_cli_options(monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["track-yaml-auto-populate", *arguments])
    with pytest.raises(SystemExit) as exception:
        populate.main()
    assert exception.value.code == 2
