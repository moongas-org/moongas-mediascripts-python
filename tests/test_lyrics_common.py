from pathlib import Path

import pytest

from mediascripts.lyrics import lyrics_common


@pytest.mark.parametrize("suffix", [".mp3", ".yml", ".yaml"])
def test_lyrics_paths_strip_only_final_extension(tmp_path: Path, suffix: str) -> None:
    track = tmp_path / f"01 - Track.live{suffix}"
    assert lyrics_common.lyrics_text_path_for(track) == tmp_path / "01 - Track.live.txt"
    assert lyrics_common.lyrics_lrc_path_for(track) == tmp_path / "01 - Track.live.lrc"


@pytest.mark.parametrize("suffix", [".txt", ".lrc"])
def test_save_lyrics_publishes_utf8_without_temporary_files(tmp_path: Path, suffix: str) -> None:
    destination = tmp_path / f"Track{suffix}"
    content = "Lyrics\nSecond line\n"
    lyrics_common.save_lyrics_file(destination, content)
    assert destination.read_text(encoding="utf-8") == content
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("existing", ["file", "symlink", "broken-symlink"])
def test_save_lyrics_never_replaces_existing_paths(tmp_path: Path, existing: str) -> None:
    destination = tmp_path / "Track.txt"
    original = tmp_path / "original.txt"
    if existing == "file":
        destination.write_text("Existing")
    else:
        if existing == "symlink":
            original.write_text("Existing")
        destination.symlink_to(original)
    with pytest.raises(FileExistsError):
        lyrics_common.save_lyrics_file(destination, "Replacement")
    if existing == "file":
        assert destination.read_text() == "Existing"
    else:
        assert destination.is_symlink()
        if existing == "symlink":
            assert original.read_text() == "Existing"
        else:
            assert not original.exists()
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("interrupted", [False, True])
def test_save_lyrics_cleans_up_and_propagates_publish_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool,
) -> None:
    destination = tmp_path / "Track.lrc"
    error = KeyboardInterrupt() if interrupted else OSError("Disk failure")

    def fail_link(source: Path, target: Path) -> None:
        assert source.read_text(encoding="utf-8") == "Lyrics\n"
        assert target == destination
        raise error

    monkeypatch.setattr(lyrics_common.os, "link", fail_link)
    with pytest.raises(type(error)):
        lyrics_common.save_lyrics_file(destination, "Lyrics\n")
    assert not list(tmp_path.iterdir())
