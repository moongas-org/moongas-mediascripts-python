import logging
from pathlib import Path

import pytest

from mediascripts.artist.artist_yaml import (
    artist_yaml_auto_populate as populate,
    artist_yaml_common as common,
)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("Music/Various Artists/Album", True),
        ("Music/My Various Artists Collection/Album", True),
        ("Music/various artists/Album", False),
        ("Music/Artist/Album", False),
    ],
)
def test_exclusions_preserve_substring_and_case_rules(path: str, expected: bool) -> None:
    assert common.excluded(Path(path)) is expected


def test_discovery_and_reference_examples_keep_letter_filtering(tmp_path: Path) -> None:
    for artist in ("Alpha", "Beta"):
        path = tmp_path / "Music" / artist / "artist.yml"
        path.parent.mkdir(parents=True)
        path.write_text(artist, encoding="utf-8")
    alpha = tmp_path / "Music" / "Alpha" / "artist.yml"
    assert common.load_input_files(tmp_path, ["A"]) == [alpha]
    assert common.load_reference_examples(tmp_path, ["A"]) == "Example: Music/Alpha/artist.yml\nAlpha"
    assert populate.load_input_files is common.load_input_files
    assert populate.load_reference_examples is common.load_reference_examples


def test_shared_backup_cleanup_preserves_other_files(tmp_path: Path) -> None:
    backup = tmp_path / "artist.yml.bak"
    backup.touch()
    source = tmp_path / "artist.yml"
    source.touch()
    assert common.clean_backup_files(tmp_path) == 1
    assert not backup.exists()
    assert source.exists()


def test_auto_populate_clean_uses_shared_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    backup = data / "artist.yml.bak"
    backup.touch()
    monkeypatch.setattr(populate, "MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    populate.clean()
    assert not backup.exists()
    assert populate.clean_backup_files is common.clean_backup_files


@pytest.mark.parametrize("continue_on_error", [False, True])
def test_cleanup_error_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    continue_on_error: bool,
) -> None:
    backup = tmp_path / "artist.yml.bak"
    backup.touch()

    def deny_unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "unlink", deny_unlink)
    if continue_on_error:
        with caplog.at_level(logging.WARNING):
            assert common.clean_backup_files(tmp_path, continue_on_error=True) == 0
        assert "Failed to remove backup file" in caplog.text
    else:
        with pytest.raises(PermissionError, match="denied"):
            common.clean_backup_files(tmp_path)
    assert backup.exists()
