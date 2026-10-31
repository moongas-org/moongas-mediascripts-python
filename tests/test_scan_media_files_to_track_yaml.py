import logging
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from mediascripts.track.track_yaml import scan_media_files_to_track_yaml as scan

TRACK = """\
trackData:
  title: Track
  artist: Artist
  albumartist: Artist
  album: Album
  genre: Rock
  year: 1995
"""


@pytest.fixture
def track_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    collection_root = tmp_path / "collection"
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(collection_root))
    directory = collection_root / "data" / "Music" / "Artist" / "Album [1995]"
    directory.mkdir(parents=True)
    path = directory / "Some Song.yml"
    path.write_text(TRACK, encoding="utf-8")
    (directory / "Some Song.mp3").write_bytes(b"fake audio")
    return path


def fake_audio(tags: dict[str, list[str]] | None = None, duration: float = 123.5) -> Mock:
    audio = Mock()
    audio.tags = tags or {}
    audio.info = SimpleNamespace(length=duration)
    return audio


def mock_audio_file(monkeypatch: pytest.MonkeyPatch, audio: Mock) -> None:
    monkeypatch.setattr(scan.mutagen, "File", lambda path, easy: audio)


def test_scan_creates_single_media_file_entry_and_populates_metadata(
    track_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_audio_file(
        monkeypatch,
        fake_audio(
            {
                "title": ["Tagged title"],
                "artist": ["Tagged artist"],
                "albumartist": ["Tagged album artist"],
                "album": ["Tagged album"],
                "genre": ["Jazz"],
                "date": ["2001-02-03"],
            }
        ),
    )

    assert scan.process_track_yaml(track_yaml)

    document = yaml.safe_load(track_yaml.read_text(encoding="utf-8"))
    assert len(document["mediaFileData"]) == 1
    data = document["mediaFileData"][0]
    media_path = track_yaml.with_suffix(".mp3")
    assert {key: value for key, value in data.items() if key != "modTime"} == {
        "path": "/data/Music/Artist/Album [1995]/Some Song.mp3",
        "size": media_path.stat().st_size,
        "format": "mp3",
        "title": "Tagged title",
        "artist": "Tagged artist",
        "albumartist": "Tagged album artist",
        "album": "Tagged album",
        "genre": "Jazz",
        "year": 2001,
        "duration": 123.5,
    }
    assert isinstance(data["modTime"], datetime)


def test_scan_falls_back_to_track_metadata_and_preserves_analysis_data(
    track_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track_yaml.write_text(
        TRACK
        + "mediaFileData:\n"
        + "  - fpcalcData: {duration: 123.5, fingerprint: retained}\n"
        + "    acoustidData: {status: ok, results: []}\n"
        + "    albumPath: stale album path\n"
        + "    artistPath: stale artist path\n"
        + "    format: flac\n"
        + "    title: Previous title\n",
        encoding="utf-8",
    )
    mock_audio_file(monkeypatch, fake_audio())

    assert scan.process_track_yaml(track_yaml)

    entry = yaml.safe_load(track_yaml.read_text(encoding="utf-8"))["mediaFileData"][0]
    assert entry["format"] == "mp3"
    assert entry["title"] == "Track"
    assert entry["artist"] == "Artist"
    assert entry["year"] == 1995
    assert "albumPath" not in entry
    assert "artistPath" not in entry
    assert entry["fpcalcData"] == {"duration": 123.5, "fingerprint": "retained"}
    assert entry["acoustidData"] == {"status": "ok", "results": []}


def test_scan_updates_first_matching_basename_media_file(
    track_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_media = track_yaml.with_suffix(".flac")
    first_media.write_bytes(b"first")
    audio = fake_audio()
    monkeypatch.setattr(scan.mutagen, "File", lambda path, easy: audio if path == first_media else None)

    assert scan.process_track_yaml(track_yaml)

    entry = yaml.safe_load(track_yaml.read_text(encoding="utf-8"))["mediaFileData"][0]
    assert entry["path"] == "/data/Music/Artist/Album [1995]/Some Song.flac"
    assert entry["format"] == "flac"


def test_dry_run_shows_meld_proposal_without_changing_yaml_or_backup(
    track_yaml: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setattr(scan.tempfile, "tempdir", str(tmp_path))
    mock_audio_file(monkeypatch, fake_audio())
    original = track_yaml.read_bytes()

    with caplog.at_level(logging.INFO):
        assert scan.process_track_yaml(track_yaml, dry_run=True)

    assert track_yaml.read_bytes() == original
    assert not track_yaml.with_name(track_yaml.name + ".bak").exists()
    output = capsys.readouterr().out
    assert "meld " in output
    assert "[dry-run] Source and backups unchanged" in caplog.text


def test_multiple_media_file_entries_are_rejected_without_overwrite(
    track_yaml: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    original = TRACK + "mediaFileData:\n  - path: first.mp3\n  - path: second.mp3\n"
    track_yaml.write_text(original, encoding="utf-8")
    mock_audio_file(monkeypatch, fake_audio())

    assert not scan.process_track_yaml(track_yaml)

    assert "Expected at most one mediaFileData entry per track" in caplog.text
    assert track_yaml.read_text(encoding="utf-8") == original


def test_missing_matching_media_file_is_reported(track_yaml: Path) -> None:
    track_yaml.with_suffix(".mp3").unlink()

    assert not scan.process_track_yaml(track_yaml)


def test_media_file_outside_collection_root_is_rejected(
    track_yaml: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(track_yaml.parents[4] / "other-root"))
    mock_audio_file(monkeypatch, fake_audio())

    assert not scan.process_track_yaml(track_yaml)
    assert "Media file is outside the collection root" in caplog.text


def test_count_zero_does_not_process_files(
    track_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["scan-media-files-to-track-yaml", "--dir", str(track_yaml.parents[2]), "--count", "0"],
    )
    monkeypatch.setattr(
        scan, "process_track_yaml", lambda *args, **kwargs: pytest.fail("Unexpected processing")
    )
    assert scan.main() == 0


def test_file_argument_rejects_non_yaml_file(track_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_path = track_yaml.with_suffix(".mp3")
    monkeypatch.setattr(sys, "argv", ["scan-media-files-to-track-yaml", "--file", str(media_path)])

    assert scan.main() == 1
