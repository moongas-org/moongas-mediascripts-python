import logging
import sys
from pathlib import Path

import pytest

from mediascripts.lyrics.lyrics_text import lyrics_text_auto_populate


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
