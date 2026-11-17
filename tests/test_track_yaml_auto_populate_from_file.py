import copy
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from collections.abc import Iterator
from email.message import Message
from email.utils import format_datetime
from io import BytesIO
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

import pytest
from mediascan.types.album.album_path import AlbumPathBuilder
from mediascan.types.track.track_yaml_file_loader import load_track_yaml_file
from mediascan.types.track.track_yaml_file_validator import validate_track_yaml_content
from mutagen.id3 import ID3, TIT2, TPE1
from openai import OpenAI

from mediascripts.track.track_yaml import track_yaml_auto_populate_from_file as populate
from mediascripts.track.track_yaml.track_yaml_common import TextWriter, save_yaml_atomically

RELEASE_ID = "11111111-1111-4111-8111-111111111111"
RECORDING_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ID = "33333333-3333-4333-8333-333333333333"
CREDIT: list[dict[str, object]] = [{"name": "Artist", "artist": {"name": "Artist"}, "joinphrase": ""}]


@pytest.fixture
def release() -> dict[str, object]:
    return {
        "id": RELEASE_ID, "title": "Album", "date": "1995-03-04", "artist-credit": CREDIT,
        "media": [{
            "position": 1, "track-count": 2, "tracks": [
                {"position": 1, "title": "First", "artist-credit": CREDIT,
                 "recording": {"id": RECORDING_ID}},
                {"position": 2, "title": "Second", "artist-credit": CREDIT,
                 "recording": {"id": OTHER_ID}},
            ],
        }],
    }


def model_client(*contents: str) -> MagicMock:
    client = MagicMock(spec=OpenAI)
    responses: list[MagicMock] = []
    for content in contents:
        response = MagicMock()
        response.choices[0].message.content = content
        responses.append(response)
    client.chat.completions.create.side_effect = responses
    return client


def install_lookup(
    monkeypatch: pytest.MonkeyPatch, release: dict[str, object], kind: str = "album",
) -> tuple[MagicMock, MagicMock]:
    intent = f"kind: {kind}\nartist: Artist\ntitle: Album\n"
    selection = "option: 1\ngenre: Rock\n"
    candidates: dict[str, object] = {"releases": [{"id": RELEASE_ID, "title": "Album"}]}
    if kind == "track":
        candidates = {"recordings": [{
            "id": RECORDING_ID, "title": "First", "releases": [{"id": RELEASE_ID}],
        }]}
    client = model_client(intent, selection)
    monkeypatch.setattr(populate, "get_client", lambda: cast(OpenAI, client))
    network = MagicMock(side_effect=[
        BytesIO(json.dumps(candidates).encode()), BytesIO(json.dumps(release).encode()),
    ])
    monkeypatch.setattr(populate, "urlopen", network)
    monkeypatch.setattr(populate.time, "sleep", MagicMock())
    return client, network


def test_album_end_to_end(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("\ufeff\n  Artist - Album (album)  \n\n", encoding="utf-8")
    destination = tmp_path / "new" / "Music"
    client, network = install_lookup(monkeypatch, release)
    assert populate.main_loop(source, destination, sleep_between_files=0)
    album_dir = destination / "Artist" / "Album [1995]"
    assert AlbumPathBuilder.of(album_dir).valid
    assert sorted(path.name for path in album_dir.iterdir()) == ["01 - First.yml", "02 - Second.yml"]
    for path in album_dir.iterdir():
        document = validate_track_yaml_content(path.read_text())
        assert document == {"trackData": {
            "title": path.stem.split(" - ")[1], "artist": "Artist",
            "albumartist": "Artist", "album": "Album", "genre": "Rock", "year": 1995,
        }}
        assert load_track_yaml_file(path).track_data.album == "Album"
    assert network.call_count == 2
    assert "query=artist" in network.call_args_list[0].args[0].full_url
    assert "recordings%2Bartist-credits" in network.call_args_list[1].args[0].full_url
    assert client.chat.completions.create.call_count == 2
    assert "Artist - Album (album)" in client.chat.completions.create.call_args_list[0].kwargs["messages"][1]["content"]
    assert not list(destination.rglob("*.bak"))
    assert source.read_text(encoding="utf-8").startswith("\ufeff")


def test_track_request_only_writes_selected_recording(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - First\n")
    install_lookup(monkeypatch, release, kind="track")
    destination = tmp_path / "Music"
    assert populate.main_loop(source, destination)
    assert [path.name for path in destination.rglob("*.yml")] == ["01 - First.yml"]


@pytest.mark.parametrize("first_directory", ["/data/Music", "/data/MusicOther", "data/Custom"])
def test_default_collection_destination(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch, first_directory: str,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - Album\n")
    (tmp_path / "mediascan-config.yml").write_text(
        f"mediaDirs:\n  - {first_directory}\n  - /data/NotSelected\n", encoding="utf-8",
    )
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    install_lookup(monkeypatch, release)
    assert populate.main_loop(source)
    assert (tmp_path / first_directory.lstrip("/") / "Artist/Album [1995]/01 - First.yml").is_file()
    assert not (tmp_path / "data/NotSelected").exists()


@pytest.mark.parametrize("config", [
    "", "mediaDirs: []", "mediaDirs: null", "mediaDirs: /data/Music",
    "mediaDirs: [null]", "mediaDirs: ['']", "mediaDirs: ['   ']",
    "mediaDirs: ['/../outside']", "mediaDirs: ['/']", "other: value",
])
def test_invalid_destination_config_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - Album\n")
    (tmp_path / "mediascan-config.yml").write_text(config)
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    client = MagicMock()
    monkeypatch.setattr(populate, "get_client", client)
    with pytest.raises(ValueError):
        populate.main_loop(source)
    client.assert_not_called()
    assert not (tmp_path / "data").exists()


def test_missing_destination_config_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - Album\n")
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="mediascan-config.yml"):
        populate.main_loop(source, llm_enabled=False)


def test_explicit_destination_does_not_require_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - Album\n")
    monkeypatch.delenv("MOONGAS_COLLECTION_ROOTDIR", raising=False)
    assert populate.main_loop(source, tmp_path / "override", llm_enabled=False)


def test_multidisc_compilation_and_safe_names(tmp_path: Path, release: dict[str, object]) -> None:
    release["title"] = "../Album: A/B & More?"
    release["artist-credit"] = [{"name": "Various Artists"}]
    media = cast(list[dict[str, object]], release["media"])
    media.append(copy.deepcopy(media[0]))
    media[1]["position"] = 2
    tracks = populate.build_tracks(release, None, "Rock", tmp_path)
    assert len(tracks) == 4
    assert [path.name for path, _ in tracks] == [
        "01.01 - First.yml", "01.02 - Second.yml", "02.01 - First.yml", "02.02 - Second.yml",
    ]
    assert all(path.is_relative_to(tmp_path / "Various Artists") for path, _ in tracks)
    assert validate_track_yaml_content(tracks[0][1])["trackData"] == {
        "title": "First", "artist": "Artist", "albumartist": "Various Artists",
        "album": "../Album: A/B & More?", "genre": "Rock", "year": 1995,
    }
    populate.save_tracks(tracks, tmp_path)
    assert AlbumPathBuilder.of(tracks[0][0].parent).valid


@pytest.mark.parametrize("extension", [".yml", ".yaml"])
def test_existing_skip_and_force_preserve_identifiers_and_media(
    tmp_path: Path, release: dict[str, object], extension: str,
) -> None:
    tracks = populate.build_tracks(release, RECORDING_ID, "Rock", tmp_path)
    path = tracks[0][0].with_suffix(extension)
    path.parent.mkdir(parents=True)
    document = validate_track_yaml_content(tracks[0][1])
    document["mediaFileData"] = [{"fpcalcData": {"duration": 12.5, "fingerprint": "known"}}]
    document["trackIdData"] = {"streamingData": [{
        "streamingService": "spotify", "trackUrl": "https://open.spotify.com/track/known",
        "trackId": "known", "apiData": None,
    }]}
    original = populate.dump_yaml(document).replace("genre: Rock", "genre: Pop")
    path.write_text(original)
    path.chmod(0o640)
    populate.save_tracks(tracks, tmp_path)
    assert path.read_text() == original
    assert not path.with_name(path.name + ".bak").exists()
    populate.save_tracks(tracks, tmp_path, force=True)
    assert path.with_name(path.name + ".bak").read_text() == original
    updated = validate_track_yaml_content(path.read_text())
    assert updated["mediaFileData"] == document["mediaFileData"]
    assert updated["trackIdData"] == document["trackIdData"]
    assert cast(dict[str, object], updated["trackData"])["genre"] == "Rock"
    assert path.stat().st_mode & 0o777 == 0o640
    assert len(list(path.parent.glob("*.y*ml"))) == 1


def test_dry_run_has_no_filesystem_changes(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - Album\n")
    install_lookup(monkeypatch, release)
    destination = tmp_path / "new/Music"
    assert populate.main_loop(source, destination, dry_run=True)
    assert not destination.exists()
    assert "+trackData:" in capsys.readouterr().out


@pytest.mark.parametrize("llm_enabled,count", [(False, None), (True, 0)])
def test_no_lookup_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, llm_enabled: bool, count: int | None,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("\nArtist - Album\n")
    client = MagicMock()
    network = MagicMock()
    monkeypatch.setattr(populate, "get_client", client)
    monkeypatch.setattr(populate, "urlopen", network)
    assert populate.main_loop(source, tmp_path / "new", llm_enabled=llm_enabled, count=count)
    client.assert_not_called()
    network.assert_not_called()
    assert not (tmp_path / "new").exists()


def test_failed_line_continues_and_count_includes_failure(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text(" \nunknown\nArtist - Album\nnot processed\n")
    client, network = install_lookup(monkeypatch, release)
    failure = MagicMock()
    failure.choices[0].message.content = "error: No identifiable artist"
    client.chat.completions.create.side_effect = [
        failure, *list(client.chat.completions.create.side_effect),
    ]
    assert not populate.main_loop(source, tmp_path / "Music", count=2, sleep_between_files=7)
    assert network.call_count == 2
    assert len(list((tmp_path / "Music").rglob("*.yml"))) == 2
    assert f"{source}:2" in caplog.text
    assert client.chat.completions.create.call_count == 3
    cast(MagicMock, populate.time.sleep).assert_any_call(7)
    assert source.read_text() == " \nunknown\nnot processed\n"
    assert source.with_name("todo.txt.bak").read_text() == " \nunknown\nArtist - Album\nnot processed\n"


@pytest.mark.parametrize("selection", [
    f"releaseId: {OTHER_ID}\ngenre: Rock",
    "error: Ambiguous request",
    "[]",
    "",
])
def test_untrusted_or_unresolved_selection_rejected(
    monkeypatch: pytest.MonkeyPatch, release: dict[str, object], selection: str,
) -> None:
    client, network = install_lookup(monkeypatch, release)
    client.chat.completions.create.side_effect = model_client(
        "kind: album\nartist: Artist\ntitle: Album", *([selection] * populate.MAX_SELECTION_ATTEMPTS),
    ).chat.completions.create.side_effect
    with pytest.raises(ValueError):
        populate.resolve_request("Album by Artist", cast(OpenAI, client), populate.MusicBrainz())
    assert network.call_count == 1


def test_recording_must_belong_to_search_results(
    monkeypatch: pytest.MonkeyPatch, release: dict[str, object],
) -> None:
    client, network = install_lookup(monkeypatch, release, kind="track")
    client.chat.completions.create.side_effect = model_client(
        "kind: track\nartist: Artist\ntitle: First",
        *([f"releaseId: {RELEASE_ID}\nrecordingId: {OTHER_ID}\ngenre: Rock"] * populate.MAX_SELECTION_ATTEMPTS),
    ).chat.completions.create.side_effect
    with pytest.raises(ValueError, match="recording absent"):
        populate.resolve_request("First by Artist", cast(OpenAI, client), populate.MusicBrainz())
    assert network.call_count == 1


def test_numbered_selection_maps_to_catalogue_ids_and_removes_queue_entry(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - First\n")
    client, network = install_lookup(monkeypatch, release, kind="track")
    client.chat.completions.create.side_effect = model_client(
        "kind: track\nartist: Artist\ntitle: First",
        "option: 1\ngenre: Rock",
    ).chat.completions.create.side_effect
    assert populate.main_loop(source, tmp_path / "Music")
    assert source.read_text() == ""
    assert (tmp_path / "Music/Artist/Album [1995]/01 - First.yml").is_file()
    prompt = client.chat.completions.create.call_args_list[1].kwargs["messages"][1]["content"]
    assert '"option": 1' in prompt
    assert RECORDING_ID not in prompt
    assert RELEASE_ID not in prompt
    assert f"/release/{RELEASE_ID}?" in network.call_args_list[1].args[0].full_url


def test_invalid_selection_retries_and_can_recover(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - First\n")
    client, network = install_lookup(monkeypatch, release, kind="track")
    client.chat.completions.create.side_effect = model_client(
        "kind: track\nartist: Artist\ntitle: First",
        "option: 9\ngenre: Rock",
        "option: 1\ngenre: Rock",
    ).chat.completions.create.side_effect
    with caplog.at_level(logging.WARNING):
        assert populate.main_loop(source, tmp_path / "Music")
    assert source.read_text() == ""
    assert client.chat.completions.create.call_count == 3
    assert "retry" in caplog.text.lower()
    assert network.call_count == 2


@pytest.mark.parametrize("bad_option", ["0", "2", "true", "'1'", "-1"])
def test_invalid_options_leave_input_unchanged(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, bad_option: str,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - First\n")
    client, network = install_lookup(monkeypatch, release, kind="track")
    client.chat.completions.create.side_effect = model_client(
        "kind: track\nartist: Artist\ntitle: First",
        *([f"option: {bad_option}\ngenre: Rock"] * populate.MAX_SELECTION_ATTEMPTS),
    ).chat.completions.create.side_effect
    assert not populate.main_loop(source, tmp_path / "Music")
    assert source.read_text() == "Artist - First\n"
    assert network.call_count == 1
    assert "after 3 selection attempts" in caplog.text
    assert not (tmp_path / "Music").exists()


def test_numbered_selection_preserves_recording_release_pair() -> None:
    candidates: list[dict[str, object]] = [
        {"id": RECORDING_ID, "title": "First", "artist-credit": [{"artist": {"id": OTHER_ID, "name": "Artist"}}],
         "releases": [{"id": RELEASE_ID, "title": "First release"}]},
        {"id": OTHER_ID, "title": "First (live)", "releases": [{"id": OTHER_ID, "title": "Live"}]},
    ]
    options, identifiers = populate.selection_options(candidates, "track")
    assert [option["option"] for option in options] == [1, 2]
    assert RECORDING_ID not in json.dumps(options)
    assert OTHER_ID not in json.dumps(options)
    assert populate.select_catalogue_option(
        cast(OpenAI, model_client("option: 2\ngenre: Rock")), "options", identifiers, "track"
    ) == (OTHER_ID, OTHER_ID, "Rock")


def test_invalid_legacy_recording_id_retries_without_looking_up_wrong_recording(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - First\n")
    client, network = install_lookup(monkeypatch, release, kind="track")
    client.chat.completions.create.side_effect = model_client(
        "kind: track\nartist: Artist\ntitle: First",
        f"recordingId: {OTHER_ID}\nreleaseId: {RELEASE_ID}\ngenre: Rock",
        "option: 1\ngenre: Rock",
    ).chat.completions.create.side_effect
    assert populate.main_loop(source, tmp_path / "Music")
    assert source.read_text() == ""
    assert network.call_count == 2
    assert f"/release/{RELEASE_ID}?" in network.call_args_list[1].args[0].full_url


def test_network_failure_reported_per_line(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - Album\n")
    _, network = install_lookup(monkeypatch, release)
    network.side_effect = URLError("unavailable")
    assert not populate.main_loop(source, tmp_path / "Music")
    assert "unavailable" in caplog.text
    assert not (tmp_path / "Music").exists()


@pytest.mark.parametrize("change", ["date", "tracks", "recording", "position", "artist-credit"])
def test_invalid_release_writes_nothing(
    tmp_path: Path, release: dict[str, object], change: str,
) -> None:
    if change == "date":
        release["date"] = "unknown"
    elif change == "artist-credit":
        release["artist-credit"] = []
    else:
        media = cast(list[dict[str, object]], release["media"])
        entries = cast(list[dict[str, object]], media[0]["tracks"])
        if change == "tracks":
            entries.pop()
        else:
            entries[-1][change] = None
    with pytest.raises(ValueError):
        populate.build_tracks(release, None, "Rock", tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_symlink_escape_and_duplicate_yaml_rejected(tmp_path: Path, release: dict[str, object]) -> None:
    destination = tmp_path / "Music"
    outside = tmp_path / "outside"
    destination.mkdir()
    outside.mkdir()
    (destination / "Artist").symlink_to(outside, target_is_directory=True)
    tracks = populate.build_tracks(release, None, "Rock", destination)
    with pytest.raises(ValueError, match="escapes"):
        populate.save_tracks(tracks, destination)
    assert not list(outside.iterdir())
    (destination / "Artist").unlink()
    path = tracks[0][0]
    path.parent.mkdir(parents=True)
    path.write_text(tracks[0][1])
    path.with_suffix(".yaml").write_text(tracks[0][1])
    with pytest.raises(ValueError, match="Conflicting"):
        populate.save_tracks(tracks, destination, force=True)
    assert not tracks[1][0].exists()


def test_create_only_write_does_not_clobber_and_cleans_up(tmp_path: Path) -> None:
    path = tmp_path / "new.yml"

    def write_first(stream: TextWriter) -> None:
        stream.write("first\n")

    def write_second(stream: TextWriter) -> None:
        stream.write("second\n")

    save_yaml_atomically(path, write_first, create_only=True)
    assert path.read_text() == "first\n"
    with pytest.raises(FileExistsError):
        save_yaml_atomically(path, write_second, create_only=True)
    assert path.read_text() == "first\n"
    assert not list(tmp_path.glob(".*.tmp"))


def test_musicbrainz_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(populate.time, "monotonic", MagicMock(side_effect=[10.0, 10.25, 11.01]))
    sleep = MagicMock()
    monkeypatch.setattr(populate.time, "sleep", sleep)
    monkeypatch.setattr(populate, "urlopen", MagicMock(side_effect=[BytesIO(b"{}"), BytesIO(b"{}")]))
    catalogue = populate.MusicBrainz()
    catalogue.get("release", query="test")
    catalogue.get("release", query="test")
    sleep.assert_called_once_with(pytest.approx(populate.MIN_SLEEP - 0.25))


@pytest.mark.parametrize("args", [["--file", "todo.txt", "--sleep", "-1"], ["--file", "todo.txt", "--count", "-1"]])
def test_cli_rejects_invalid_args(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["track-yaml-auto-populate-from-file", *args])
    with pytest.raises(SystemExit) as error:
        populate.main()
    assert error.value.code == 2


def test_cli_passes_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = MagicMock(return_value=True)
    monkeypatch.setattr(populate, "main_loop", runner)
    monkeypatch.setattr(sys, "argv", [
        "track-yaml-auto-populate-from-file", "--file", str(tmp_path / "todo.txt"),
        "--dir", str(tmp_path), "--sleep", "0", "--llm=false", "--force", "-n", "-c", "3",
    ])
    assert populate.main() == 0
    runner.assert_called_once_with(tmp_path / "todo.txt", tmp_path, 0, False, True, True, 3)


def test_cli_default_file_uses_collection_root(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "todo.txt").write_text("Artist - Album\n")
    (tmp_path / "mediascan-config.yml").write_text("mediaDirs: [/data/Custom]\n")
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["track-yaml-auto-populate-from-file"])
    install_lookup(monkeypatch, release)
    assert populate.main() == 0
    assert (tmp_path / "data/Custom/Artist/Album [1995]/01 - First.yml").is_file()


def test_cli_default_file_requires_collection_root(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("MOONGAS_COLLECTION_ROOTDIR", raising=False)
    monkeypatch.setattr(sys, "argv", ["track-yaml-auto-populate-from-file", "--dir", "/unused"])
    assert populate.main() == 1
    assert "MOONGAS_COLLECTION_ROOTDIR must be set, or use --file" in caplog.text


def test_cli_missing_default_file_reports_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["track-yaml-auto-populate-from-file"])
    assert populate.main() == 1
    assert f"Input file does not exist: {tmp_path / 'todo.txt'}" in caplog.text


def test_logging_covers_lookup_and_write_lifecycle(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=populate.__name__)
    source = tmp_path / "todo.txt"
    source.write_text("Artist - Album\n")
    (tmp_path / "mediascan-config.yml").write_text("mediaDirs: [/data/Custom]\n")
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    monkeypatch.setenv("LLM_API_KEY", "secret-not-for-logs")
    install_lookup(monkeypatch, release)
    assert populate.main_loop(source)
    for message in [
        "Reading destination configuration:", "Selected first mediaDirs entry",
        "Starting batch:", "Reading UTF-8 request file:", "Initializing OpenAI-compatible client",
        "Interpreting request with LLM", "Sending LLM request:", "LLM response received in",
        "Interpreted request: kind=album", "MusicBrainz lookup:", "MusicBrainz response: HTTP",
        "MusicBrainz lookup completed in", "MusicBrainz search returned 1 release candidate(s)",
        "Selecting matching release with LLM", "Validated selection:", "Retrieved selected release:",
        "Building track YAML:", "Prepared 2 schema-validated track YAML file(s)",
        "Creating missing artist/album directories:", "Saved track YAML:",
        "Track file summary: 2 created, 0 updated, 0 skipped, 0 unchanged, 0 previewed",
        f"[{source}:1] Completed in", "1 processed, 1 completed, 0 failed, 0 listed",
    ]:
        assert message in caplog.text
    assert "secret-not-for-logs" not in caplog.text
    assert "LLM response content:" not in caplog.text
    assert "MusicBrainz response content:" not in caplog.text


def test_logging_reports_write_modes(
    tmp_path: Path, release: dict[str, object], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=populate.__name__)
    tracks = populate.build_tracks(release, RECORDING_ID, "Rock", tmp_path)
    populate.save_tracks(tracks, tmp_path, dry_run=True)
    assert "0 created, 0 updated, 0 skipped, 0 unchanged, 1 previewed" in caplog.text
    populate.save_tracks(tracks, tmp_path)
    populate.save_tracks(tracks, tmp_path)
    assert "0 created, 0 updated, 1 skipped, 0 unchanged, 0 previewed" in caplog.text
    path = tracks[0][0]
    path.write_text(path.read_text().replace("genre: Rock", "genre: Pop"))
    populate.save_tracks(tracks, tmp_path, force=True)
    assert "Original track YAML backed up to" in caplog.text
    assert "0 created, 1 updated, 0 skipped, 0 unchanged, 0 previewed" in caplog.text
    populate.save_tracks(tracks, tmp_path, force=True)
    assert "0 created, 0 updated, 0 skipped, 1 unchanged, 0 previewed" in caplog.text


def test_debug_logging_includes_response_details_and_failure_traceback(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=populate.__name__)
    source = tmp_path / "todo.txt"
    source.write_text("\nArtist - Album\n")
    client, _ = install_lookup(monkeypatch, release)
    assert populate.main_loop(source, tmp_path / "Music", dry_run=True)
    for message in [
        "Ignoring blank line", "LLM response content:", "MusicBrainz response content:",
        "MusicBrainz rate-limit wait:", "Validated proposed track YAML:",
    ]:
        assert message in caplog.text
    client.chat.completions.create.side_effect = ValueError("provider unavailable")
    assert not populate.main_loop(source, tmp_path / "Music")
    assert "1 processed, 0 completed, 1 failed, 0 listed" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text


def test_input_backup_precedes_lookup_and_preserves_exact_bytes(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=populate.__name__)
    source = tmp_path / "todo.txt"
    original = b"\xef\xbb\xbf \r\nArtist - Album\r\n\r\nkeep this without newline"
    source.write_bytes(original)
    source.chmod(0o640)
    backup = source.with_name("todo.txt.bak")
    backup.write_text("previous run")
    install_lookup(monkeypatch, release)

    def resolve(_text: str, _client: OpenAI, _catalogue: populate.MusicBrainz) -> tuple[dict[str, object], None, str]:
        assert backup.read_bytes() == original
        assert source.read_bytes() == original
        return release, None, "Rock"

    monkeypatch.setattr(populate, "resolve_request", resolve)
    assert populate.main_loop(source, tmp_path / "Music", count=1)
    assert source.read_bytes() == b"\xef\xbb\xbf \r\n\r\nkeep this without newline"
    assert source.stat().st_mode & 0o777 == 0o640
    assert backup.read_bytes() == original
    assert "Input file backed up to" in caplog.text
    assert f"[{source}:2] Removed successfully processed input line" in caplog.text


def test_duplicate_lines_removed_by_position_not_value(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("same request\nsame request\n")
    install_lookup(monkeypatch, release)
    monkeypatch.setattr(populate, "resolve_request", MagicMock(side_effect=[
        ValueError("first request failed"), (release, None, "Rock"),
    ]))
    assert not populate.main_loop(source, tmp_path / "Music", sleep_between_files=0)
    assert source.read_text() == "same request\n"
    assert source.with_name("todo.txt.bak").read_text() == "same request\nsame request\n"


def test_existing_track_skip_still_removes_successful_input_line(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - Album\n")
    install_lookup(monkeypatch, release)
    tracks = populate.build_tracks(release, None, "Rock", tmp_path / "Music")
    populate.save_tracks(tracks, tmp_path / "Music")
    original_tracks = [path.read_bytes() for path, _ in tracks]
    assert populate.main_loop(source, tmp_path / "Music")
    assert source.read_bytes() == b""
    assert [path.read_bytes() for path, _ in tracks] == original_tracks


@pytest.mark.parametrize("mode", ["dry-run", "llm-disabled", "count-zero"])
def test_read_only_modes_preserve_input_and_backup(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    source = tmp_path / "todo.txt"
    original = b"Artist - Album\r\n"
    source.write_bytes(original)
    backup = source.with_name("todo.txt.bak")
    backup.write_text("keep existing backup")
    install_lookup(monkeypatch, release)
    assert populate.main_loop(
        source, tmp_path / "Music", dry_run=mode == "dry-run",
        llm_enabled=mode != "llm-disabled", count=0 if mode == "count-zero" else None,
    )
    assert source.read_bytes() == original
    assert backup.read_text() == "keep existing backup"


def test_failed_track_write_retains_input(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - Album\n")
    install_lookup(monkeypatch, release)
    monkeypatch.setattr(populate, "save_tracks", MagicMock(side_effect=OSError("disk full")))
    assert not populate.main_loop(source, tmp_path / "Music")
    assert source.read_text() == "Artist - Album\n"
    assert source.with_name("todo.txt.bak").read_bytes() == source.read_bytes()


def test_interruption_keeps_pending_lines_and_prior_progress(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("first\nsecond\nthird\n")
    install_lookup(monkeypatch, release)
    monkeypatch.setattr(populate, "resolve_request", MagicMock(side_effect=[
        (release, None, "Rock"), KeyboardInterrupt,
    ]))
    with pytest.raises(KeyboardInterrupt):
        populate.main_loop(source, tmp_path / "Music", sleep_between_files=0)
    assert source.read_text() == "second\nthird\n"
    assert source.with_name("todo.txt.bak").read_text() == "first\nsecond\nthird\n"


def test_input_edits_during_lookup_are_not_overwritten(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("first\n")
    client, _ = install_lookup(monkeypatch, release)

    def resolve(_text: str, _client: OpenAI, _catalogue: populate.MusicBrainz) -> tuple[dict[str, object], None, str]:
        source.write_text("first\nnew request\n")
        return release, None, "Rock"

    monkeypatch.setattr(populate, "resolve_request", resolve)
    with pytest.raises(ValueError, match="Input file changed"):
        populate.main_loop(source, tmp_path / "Music")
    assert source.read_text() == "first\nnew request\n"
    assert source.with_name("todo.txt.bak").read_text() == "first\n"
    client.chat.completions.create.assert_not_called()


def test_input_update_failure_aborts_and_preserves_pending_lines(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("first\nsecond\n")
    install_lookup(monkeypatch, release)
    resolver = MagicMock(return_value=(release, None, "Rock"))
    monkeypatch.setattr(populate, "resolve_request", resolver)
    replace = populate.os.replace

    def fail_input_replace(src: str | Path, dst: str | Path) -> None:
        if Path(dst) == source:
            raise OSError("input replacement failed")
        replace(src, dst)

    monkeypatch.setattr(populate.os, "replace", fail_input_replace)
    with pytest.raises(OSError, match="input replacement failed"):
        populate.main_loop(source, tmp_path / "Music")
    assert source.read_text() == "first\nsecond\n"
    assert source.with_name("todo.txt.bak").read_bytes() == source.read_bytes()
    assert resolver.call_count == 1
    assert not list(tmp_path.glob(".*.tmp"))


def test_backup_failure_prevents_lookup_and_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - Album\n")
    client = MagicMock()
    monkeypatch.setattr(populate, "get_client", client)
    monkeypatch.setattr(populate, "save_input_snapshot", MagicMock(side_effect=OSError("backup failed")))
    with pytest.raises(OSError, match="backup failed"):
        populate.main_loop(source, tmp_path / "Music")
    client.assert_not_called()
    assert source.read_text() == "Artist - Album\n"


@pytest.mark.parametrize("symlink_target", ["input", "backup"])
def test_symlink_input_or_backup_rejected_before_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink_target: str,
) -> None:
    source = tmp_path / "todo.txt"
    target = tmp_path / "target.txt"
    target.write_text("Artist - Album\n")
    if symlink_target == "input":
        source.symlink_to(target)
    else:
        source.write_text("Artist - Album\n")
        source.with_name("todo.txt.bak").symlink_to(target)
    client = MagicMock()
    monkeypatch.setattr(populate, "get_client", client)
    with pytest.raises(ValueError, match="symlink input"):
        populate.main_loop(source, tmp_path / "Music")
    client.assert_not_called()
    assert target.read_text() == "Artist - Album\n"


@pytest.mark.parametrize("language", ["yaml", "yml", "YAML", ""])
def test_fenced_intent_and_selection_complete_successfully(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch, language: str,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Winterkalte - The Fate of the Sea\n")
    client, _ = install_lookup(monkeypatch, release, kind="track")
    client.chat.completions.create.side_effect = model_client(
        f"```{language}\nkind: track\nartist: Winterkalte\ntitle: The Fate of the Sea\n```",
        f"```{language}\noption: 1\ngenre: Industrial\n```",
    ).chat.completions.create.side_effect
    assert populate.main_loop(source, tmp_path / "Music")
    assert source.read_text() == ""
    assert len(list((tmp_path / "Music").rglob("*.yml"))) == 1


@pytest.mark.parametrize("content", [
    "Here is your YAML:\n```yaml\nkind: track\n```",
    "```yaml\nkind: track\n```\nExtra explanation",
    "```yaml\nkind: track\n",
    "```yaml\nkind: track\n```\n```yaml\nkind: album\n```",
    "```python\nkind: track\n```",
    "```yaml\n[]\n```",
    "```yaml\nkind: track\nkind: album\n```",
])
def test_fence_handling_does_not_accept_prose_or_invalid_yaml(content: str) -> None:
    client = model_client(content)
    with pytest.raises(populate.YAML_ERRORS):
        populate.ask_model(cast(OpenAI, client), populate.INTENT_PROMPT, "request")


def http_error(status: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("https://musicbrainz.org/ws/2/recording", status, "unavailable", headers, BytesIO(b"error"))


@pytest.mark.parametrize("status", sorted(populate.RETRY_STATUSES))
def test_transient_musicbrainz_http_error_retried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, status: int,
) -> None:
    network = MagicMock(side_effect=[http_error(status), BytesIO(b'{"recordings": []}')])
    monkeypatch.setattr(populate, "urlopen", network)
    sleep = MagicMock()
    monkeypatch.setattr(populate.time, "sleep", sleep)
    assert populate.MusicBrainz().get("recording", query="test") == {"recordings": []}
    assert network.call_count == 2
    sleep.assert_any_call(2.0)
    assert f"MusicBrainz HTTP {status}" in caplog.text
    assert "Retrying MusicBrainz in 2.00s" in caplog.text


@pytest.mark.parametrize("error", [URLError("offline"), TimeoutError("timeout"), ConnectionResetError("reset")])
def test_transient_connection_error_retried(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    network = MagicMock(side_effect=[error, BytesIO(b"{}")])
    monkeypatch.setattr(populate, "urlopen", network)
    monkeypatch.setattr(populate.time, "sleep", MagicMock())
    assert populate.MusicBrainz().get("release", query="test") == {}
    assert network.call_count == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_musicbrainz_error_not_retried(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    network = MagicMock(side_effect=http_error(status))
    monkeypatch.setattr(populate, "urlopen", network)
    sleep = MagicMock()
    monkeypatch.setattr(populate.time, "sleep", sleep)
    with pytest.raises(HTTPError):
        populate.MusicBrainz().get("release", query="test")
    network.assert_called_once()
    sleep.assert_not_called()


def test_retries_are_bounded_and_failed_input_retained(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - Album\n")
    _, network = install_lookup(monkeypatch, release)
    network.side_effect = [http_error(503) for _ in range(populate.MAX_ATTEMPTS)]
    assert not populate.main_loop(source, tmp_path / "Music")
    assert network.call_count == populate.MAX_ATTEMPTS
    assert source.read_text() == "Artist - Album\n"
    assert not (tmp_path / "Music").exists()
    for delay in [2.0, 4.0, 8.0]:
        cast(MagicMock, populate.time.sleep).assert_any_call(delay)


@pytest.mark.parametrize("header,expected", [("7", 7.0), ("999999", 120.0), ("0", 0.0), ("invalid", 2.0)])
def test_retry_after_seconds_respected(
    monkeypatch: pytest.MonkeyPatch, header: str, expected: float,
) -> None:
    monkeypatch.setattr(populate, "urlopen", MagicMock(side_effect=[http_error(503, header), BytesIO(b"{}")]))
    sleep = MagicMock()
    monkeypatch.setattr(populate.time, "sleep", sleep)
    assert populate.MusicBrainz().get("release", query="test") == {}
    assert sleep.call_args_list[0].args[0] == expected


def test_retry_after_http_date() -> None:
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
    assert 58 <= populate.retry_after_delay(future, 2.0) <= 60
    past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=60), usegmt=True)
    assert populate.retry_after_delay(past, 2.0) == 0


@pytest.mark.parametrize("body", [b"not JSON", b"[]"])
def test_invalid_catalogue_response_not_retried(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    network = MagicMock(return_value=BytesIO(body))
    monkeypatch.setattr(populate, "urlopen", network)
    with pytest.raises(ValueError):
        populate.MusicBrainz().get("release", query="test")
    network.assert_called_once()


@pytest.mark.parametrize("successful_stage", [1, 2])
def test_empty_search_uses_broader_fallback_then_completes(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch, successful_stage: int,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Esplendor Geometrico - Muerte a Escalia Industrial\n")
    client, network = install_lookup(monkeypatch, release, kind="track")
    client.chat.completions.create.side_effect = model_client(
        "kind: track\nartist: Esplendor Geométrico\ntitle: Muerte a Escalía Industrial",
        f"releaseId: {RELEASE_ID}\nrecordingId: {RECORDING_ID}\ngenre: Industrial",
    ).chat.completions.create.side_effect
    responses = list(network.side_effect)
    network.side_effect = [BytesIO(b'{"recordings": []}') for _ in range(successful_stage)] + responses
    assert populate.main_loop(source, tmp_path / "Music")
    assert source.read_text() == ""
    assert network.call_count == successful_stage + 2
    urls = [call.args[0].full_url for call in network.call_args_list]
    assert "Escalia~1" in urls[1]
    if successful_stage == 2:
        assert "artist" not in urls[2]
    assert client.chat.completions.create.call_count == 2


def test_all_search_strategies_exhausted_without_selection(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Converter - Conquerer\n")
    client, network = install_lookup(monkeypatch, release, kind="track")
    network.side_effect = [BytesIO(b'{"recordings": []}') for _ in range(3)]
    assert not populate.main_loop(source, tmp_path / "Music")
    assert network.call_count == 3
    assert client.chat.completions.create.call_count == 1
    assert source.read_text() == "Converter - Conquerer\n"


def test_fuzzy_search_does_not_inject_lucene_operators() -> None:
    query = populate.fuzzy_terms("recording", 'Escalía: "title" +evil* -other')
    assert query == "(recording:Escalia~1 AND recording:title~1 AND recording:evil~1 AND recording:other~1)"


def test_broad_search_can_be_rejected_without_writing(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Unknown - Title\n")
    client, network = install_lookup(monkeypatch, release)
    client.chat.completions.create.side_effect = model_client(
        "kind: album\nartist: Unknown\ntitle: Title", "error: Results belong to another artist",
    ).chat.completions.create.side_effect
    network.side_effect = [
        BytesIO(b'{"recordings": []}'), BytesIO(b'{"recordings": []}'),
        BytesIO(json.dumps({"recordings": [{"id": RECORDING_ID, "title": "Unrelated"}]}).encode()),
    ]
    assert not populate.main_loop(source, tmp_path / "Music")
    assert network.call_count == 3
    assert source.read_text() == "Unknown - Title\n"
    assert not (tmp_path / "Music").exists()


@pytest.mark.parametrize(("input_text", "model_kind", "expected"), [
    ("Artist - First", "album", "recording"),
    ("Artist - First (ALBUM)", "track", "release"),
])
def test_text_scope_is_determined_by_explicit_album_word(
    input_text: str, model_kind: str, expected: str, monkeypatch: pytest.MonkeyPatch,
    release: dict[str, object],
) -> None:
    client, network = install_lookup(monkeypatch, release, kind="track" if expected == "recording" else "album")
    responses = list(client.chat.completions.create.side_effect)
    responses[0].choices[0].message.content = f"kind: {model_kind}\nartist: Artist\ntitle: First\n"
    client.chat.completions.create.side_effect = responses
    _, recording_id, _ = populate.resolve_request(input_text, cast(OpenAI, client), populate.MusicBrainz())
    assert (recording_id is not None) == (expected == "recording")
    assert f"/{expected}?" in network.call_args_list[0].args[0].full_url


@pytest.mark.parametrize(("url", "payload", "kind", "metadata"), [
    ("https://www.youtube.com/watch?v=vabnZ9-ex7o&list=PLabc",
     b'{"title": "Artist - Track", "author_name": "Artist"}', "track", "Artist - Track"),
    ("https://youtu.be/vabnZ9-ex7o",
     b'{"title": "Artist - Track", "author_name": "Artist"}', "track", "Artist - Track"),
    ("https://www.youtube.com/playlist?list=PLabc",
     b'<script>"playlistMetadataRenderer":{"title":"Artist - Album"}</script>', "album", "Artist - Album"),
    ("https://www.shazam.com/song/123456/some-album",
     b'<link rel="canonical" href="https://www.shazam.com/song/123456/track">'
     b'<meta property="og:title" content="Track - Artist &amp; Guest">', "track", "Track - Artist & Guest"),
])
def test_url_metadata_and_scope(
    url: str, payload: bytes, kind: str, metadata: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    network = MagicMock(return_value=BytesIO(payload))
    monkeypatch.setattr(populate, "urlopen", network)
    result_kind, result_metadata = populate.request_url_metadata(url)
    assert result_kind == kind
    assert metadata in result_metadata
    assert network.call_count == 1


@pytest.mark.parametrize("url", [
    "https://youtube.com.evil.example/watch?v=vabnZ9-ex7o",
    "https://www.youtube.com/playlist?list=../../etc",
    "https://www.shazam.com/song/not-a-number",
    "https://user@www.youtube.com/watch?v=vabnZ9-ex7o",
    "file:///etc/passwd",
])
def test_unsupported_urls_are_rejected_without_fetch(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    network = MagicMock()
    monkeypatch.setattr(populate, "urlopen", network)
    with pytest.raises(ValueError):
        populate.request_url_metadata(url)
    network.assert_not_called()


def test_shazam_rejects_unrelated_canonical_song(monkeypatch: pytest.MonkeyPatch) -> None:
    page = (
        b'<link rel="canonical" href="https://www.shazam.com/song/999/other">'
        b'<meta property="og:title" content="Unrelated - Artist">'
    )
    monkeypatch.setattr(populate, "urlopen", MagicMock(return_value=BytesIO(page)))
    with pytest.raises(ValueError, match="different or unknown song"):
        populate.request_url_metadata("https://www.shazam.com/song/123/track")


def test_unavailable_popularity_keeps_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    artist_id = "44444444-4444-4444-8444-444444444444"
    source = tmp_path / "todo.txt"
    source.write_text("Artist\n")
    client = model_client("kind: artist\nartist: Artist", f"artistId: {artist_id}")
    monkeypatch.setattr(populate, "get_client", lambda: cast(OpenAI, client))
    monkeypatch.setattr(populate, "urlopen", MagicMock(side_effect=[
        BytesIO(json.dumps({"artists": [{"id": artist_id}]}).encode()), BytesIO(b"[]"),
    ]))
    with caplog.at_level(logging.WARNING):
        assert not populate.main_loop(source, tmp_path / "Music")
    assert "No ListenBrainz popularity data" in caplog.text
    assert source.read_text() == "Artist\n"


def test_artist_only_selects_highest_listen_count(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    artist_id = "44444444-4444-4444-8444-444444444444"
    source = tmp_path / "todo.txt"
    source.write_text("Artist\n")
    client = model_client(
        "kind: artist\nartist: Artist",
        f"artistId: {artist_id}",
        "option: 1\ngenre: Rock",
    )
    monkeypatch.setattr(populate, "get_client", lambda: cast(OpenAI, client))
    responses = [
        {"artists": [{"id": artist_id, "name": "Artist"}]},
        [
            {"recording_mbid": OTHER_ID, "total_listen_count": 1},
            {"recording_mbid": RECORDING_ID, "total_listen_count": 100},
        ],
        {"id": RECORDING_ID, "title": "First", "releases": [{"id": RELEASE_ID}]},
        release,
    ]
    network = MagicMock(side_effect=[BytesIO(json.dumps(item).encode()) for item in responses])
    monkeypatch.setattr(populate, "urlopen", network)
    monkeypatch.setattr(populate.time, "sleep", MagicMock())
    assert populate.main_loop(source, tmp_path / "Music")
    assert [path.name for path in (tmp_path / "Music").rglob("*.yml")] == ["01 - First.yml"]
    assert source.read_text() == ""
    assert f"top-recordings-for-artist/{artist_id}" in network.call_args_list[1].args[0].full_url
    assert f"recording/{RECORDING_ID}?" in network.call_args_list[2].args[0].full_url


def test_existing_track_in_other_collection_directory_is_success(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - First\n")
    (tmp_path / "mediascan-config.yml").write_text("mediaDirs: [/data/Music, /data/MusicOther]\n")
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    install_lookup(monkeypatch, release, kind="track")
    existing = tmp_path / "data/MusicOther/Artist/Different Album [1990]/03 - First.yml"
    existing.parent.mkdir(parents=True)
    existing.write_text("trackData:\n  title: First\n  artist: Artist\n")
    assert populate.main_loop(source)
    assert source.read_text() == ""
    assert not (tmp_path / "data/Music").exists()
    assert existing.read_text() == "trackData:\n  title: First\n  artist: Artist\n"


@pytest.mark.parametrize(("url", "scope", "page"), [
    ("https://youtube.com/watch?v=vabnZ9-ex7o&list=PLabc", "track",
     b'{"title": "Artist - First", "author_name": "Artist"}'),
    ("https://youtube.com/playlist?list=PLabc", "album",
     b'<script>"playlistMetadataRenderer":{"title":"Artist - Album"}</script>'),
    ("https://shazam.com/song/123456/album", "track",
     b'<link rel="canonical" href="https://www.shazam.com/song/123456/first">'
     b'<meta property="og:title" content="First - Artist">'),
])
def test_url_end_to_end_enforces_scope(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
    url: str, scope: str, page: bytes,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text(url + "\n")
    client, network = install_lookup(monkeypatch, release, kind=scope)
    responses = list(client.chat.completions.create.side_effect)
    responses[0].choices[0].message.content = (
        f"kind: {'track' if scope == 'album' else 'album'}\nartist: Artist\ntitle: First\n"
    )
    client.chat.completions.create.side_effect = responses
    network.side_effect = [BytesIO(page), *list(network.side_effect)]
    assert populate.main_loop(source, tmp_path / "Music")
    assert len(list((tmp_path / "Music").rglob("*.yml"))) == (2 if scope == "album" else 1)
    assert source.read_text() == ""
    assert "YouTube" in client.chat.completions.create.call_args_list[0].kwargs["messages"][1]["content"] or (
        "Shazam" in client.chat.completions.create.call_args_list[0].kwargs["messages"][1]["content"]
    )


def test_existing_media_tags_count_as_duplicate(tmp_path: Path) -> None:
    album = tmp_path / "Artist" / "Album [2000]"
    album.mkdir(parents=True)
    media = album / "track.mp3"
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["First"]))
    tags.add(TPE1(encoding=3, text=["Artist"]))
    tags.save(media)
    proposed = [(tmp_path / "unused.yml", populate.dump_yaml({"trackData": {"artist": "Artist", "title": "First"}}))]
    assert populate.find_existing_track(proposed, [tmp_path]) == media


def test_duplicate_index_reuses_previous_scan_and_finds_generated_tracks(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("first request\nsecond request\n")
    destination = tmp_path / "Music"
    client, _ = install_lookup(monkeypatch, release, kind="track")
    monkeypatch.setattr(populate, "resolve_request", MagicMock(return_value=(release, RECORDING_ID, "Rock")))
    walker = MagicMock(wraps=populate.iter_duplicate_candidates)
    monkeypatch.setattr(populate, "iter_duplicate_candidates", walker)
    assert populate.main_loop(source, destination, sleep_between_files=0)
    assert walker.call_count == 1
    assert len(list(destination.rglob("*.yml"))) == 1
    assert source.read_text() == ""
    client.chat.completions.create.assert_not_called()


def test_duplicate_scan_reports_progress_and_avoids_symlink_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    album = tmp_path / "Music" / "Artist" / "Album [2000]"
    album.mkdir(parents=True)
    (album / "loop").symlink_to(tmp_path / "Music", target_is_directory=True)
    (album / "01 - Track.yml").write_text("trackData: {artist: Other, title: Track}\n")
    proposed = [(album / "new.yml", populate.dump_yaml({"trackData": {"artist": "Artist", "title": "New"}}))]
    clock = iter([0.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0])
    monkeypatch.setattr(populate.time, "monotonic", lambda: next(clock, 12.0))
    with caplog.at_level(logging.INFO):
        assert populate.find_existing_track(proposed, [tmp_path / "Music"]) is None
    assert "Checking collection for an existing track" in caplog.text
    assert "scanned" in caplog.text.lower()
    assert "Skipping already visited" in caplog.text
    assert "No matching track found" in caplog.text


def test_duplicate_index_resumes_after_match_and_caches_all_prior_entries(tmp_path: Path) -> None:
    album = tmp_path / "Artist" / "Album [2000]"
    album.mkdir(parents=True)
    first = album / "01 - First.yml"
    second = album / "02 - Second.yml"
    first.write_text("trackData: {artist: Artist, title: First}\n")
    second.write_text("trackData: {artist: Artist, title: Second}\n")

    def proposed(title: str) -> list[tuple[Path, str]]:
        return [(album / "new.yml", populate.dump_yaml({"trackData": {"artist": "Artist", "title": title}}))]

    index = populate.ExistingTrackIndex([tmp_path])
    assert index.find(proposed("First")) == first
    assert index.scanned == 1
    assert index.find(proposed("Second")) == second
    assert index.scanned == 2
    assert index.find(proposed("First")) == first
    assert index.scanned == 2
    assert index.find(proposed("Missing")) is None
    assert index.find(proposed("First")) == first


def test_failed_duplicate_scan_does_not_turn_into_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_roots: list[Path]) -> Iterator[Path]:
        yield from ()
        raise OSError("directory read failed")

    monkeypatch.setattr(populate, "iter_duplicate_candidates", fail)
    index = populate.ExistingTrackIndex([tmp_path])
    tracks = [(tmp_path / "track.yml", populate.dump_yaml({"trackData": {"artist": "A", "title": "B"}}))]
    for _ in range(2):
        with pytest.raises(OSError, match="directory read failed"):
            index.find(tracks)


def test_existing_media_skips_request_and_removes_line(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - First\n")
    destination = tmp_path / "Music"
    album = destination / "Artist" / "Other Album [2000]"
    album.mkdir(parents=True)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["First"]))
    tags.add(TPE1(encoding=3, text=["Artist"]))
    tags.save(album / "track.mp3")
    install_lookup(monkeypatch, release, kind="track")
    assert populate.main_loop(source, destination)
    assert source.read_text() == ""
    assert not list(destination.rglob("*.yml"))


@pytest.mark.parametrize("extension", [".yml", ".yaml"])
def test_force_refreshes_existing_single_track(
    tmp_path: Path, release: dict[str, object], monkeypatch: pytest.MonkeyPatch, extension: str,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("Artist - First\n")
    destination = tmp_path / "Music"
    path, content = populate.build_tracks(release, RECORDING_ID, "Rock", destination)[0]
    path = path.with_suffix(extension)
    path.parent.mkdir(parents=True)
    original = content.replace("Rock", "Jazz")
    path.write_text(original)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["First"]))
    tags.add(TPE1(encoding=3, text=["Artist"]))
    tags.save(path.with_suffix(".mp3"))
    install_lookup(monkeypatch, release, kind="track")
    assert populate.main_loop(source, destination, force=True)
    assert load_track_yaml_file(path).track_data.genre == "Rock"
    assert path.with_name(path.name + ".bak").read_text() == original
    assert not path.with_suffix(".yml" if extension == ".yaml" else ".yaml").exists()
    assert source.read_text() == ""


def test_unresolved_request_is_warned_and_remains_in_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "todo.txt"
    source.write_text("https://unsupported.example/song\n")
    monkeypatch.setattr(populate, "get_client", lambda: cast(OpenAI, model_client()))
    with caplog.at_level(logging.WARNING):
        assert not populate.main_loop(source, tmp_path / "Music")
    assert any(record.levelno == logging.WARNING and "Unsupported request URL" in record.message for record in caplog.records)
    assert source.read_text() == "https://unsupported.example/song\n"
