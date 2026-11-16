import copy
import json
import logging
import sys
from io import BytesIO
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock
from urllib.error import URLError

import pytest
from mediascan.types.album.album_path import AlbumPathBuilder
from mediascan.types.track.track_yaml_file_loader import load_track_yaml_file
from mediascan.types.track.track_yaml_file_validator import validate_track_yaml_content
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
    selection = f"releaseId: {RELEASE_ID}\ngenre: Rock\n"
    candidates: dict[str, object] = {"releases": [{"id": RELEASE_ID, "title": "Album"}]}
    if kind == "track":
        selection += f"recordingId: {RECORDING_ID}\n"
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
        "kind: album\nartist: Artist\ntitle: Album", selection,
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
        f"releaseId: {RELEASE_ID}\nrecordingId: {OTHER_ID}\ngenre: Rock",
    ).chat.completions.create.side_effect
    with pytest.raises(ValueError, match="recording absent"):
        populate.resolve_request("First by Artist", cast(OpenAI, client), populate.MusicBrainz())
    assert network.call_count == 1


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
