import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mediascripts.track.track_yaml import track_yaml_auto_populate as populate


@pytest.fixture
def album(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "Music" / "Artist" / "Album [2000]"
    path.mkdir(parents=True)
    return path


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("scope", ["collection", "dir", "file"])
@pytest.mark.parametrize("existing", [False, True])
def test_revert_restores_only_track_backups(
    album: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    dry_run: bool,
    scope: str,
    existing: bool,
) -> None:
    track = album / "01 - Track.yml"
    if existing:
        track.write_text("current")
    backup = album / "01 - Track.yml.bak"
    backup.write_text("original")
    protected = [
        album / name
        for name in (
            "album.yml",
            "album.yml.bak",
            "artist.yml",
            "artist.yml.bak",
            "ALBUM.yml",
            "ALBUM.yml.bak",
            "files.yml.bak",
            "02 - Track.yaml.bak",
        )
    ]
    protected += [album.parent / "outside.yml.bak"]
    for path in protected:
        path.write_text("protected")
    factory = MagicMock()
    monkeypatch.setattr(populate, "get_client", factory)
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(album.parents[3]))
    arguments = ["track-yaml-auto-populate", "--revert"]
    if scope == "dir":
        arguments += ["--dir", str(album.parents[1])]
    elif scope == "file":
        arguments += ["--file", str(track)]
    if dry_run:
        arguments += ["--dry-run"]
    monkeypatch.setattr(sys, "argv", arguments)
    with caplog.at_level(logging.INFO):
        assert populate.main() == 0
    factory.assert_not_called()
    if dry_run:
        assert track.exists() is existing
        if existing:
            assert track.read_text() == "current"
        assert backup.read_text() == "original"
        assert "[dry-run] Would restore" in caplog.text
    else:
        assert track.read_text() == "original"
        assert not backup.exists()
    assert all(path.read_text() == "protected" for path in protected)


@pytest.mark.parametrize("name", ["album.yml", "artist.yml", "ALBUM.yml", "files.yml", "track.yaml"])
def test_explicit_nontrack_file_is_rejected(album: Path, name: str) -> None:
    source = album / name
    source.write_text("current")
    backup = source.with_name(name + ".bak")
    backup.write_text("original")
    with pytest.raises(ValueError, match="requires a track"):
        populate.revert(specific_file=source)
    assert source.read_text() == "current"
    assert backup.read_text() == "original"


def test_revert_follows_library_directory_symlinks(album: Path, tmp_path: Path) -> None:
    backup = album / "track.yml.bak"
    backup.write_text("original")
    library = tmp_path / "linked"
    library.mkdir()
    (library / "Music").symlink_to(album.parents[1], target_is_directory=True)
    populate.revert(specific_dir=library)
    assert (album / "track.yml").read_text() == "original"
    assert not backup.exists()


def test_symlink_backup_is_rejected(album: Path) -> None:
    protected = album / "artist.yml"
    protected.write_text("protected")
    backup = album / "track.yml.bak"
    backup.symlink_to(protected)
    with pytest.raises(ValueError, match="not a symlink"):
        populate.revert(specific_dir=album)
    assert protected.read_text() == "protected"
    assert backup.is_symlink()
    assert not (album / "track.yml").exists()


def test_failed_replacement_preserves_both_files(
    album: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = album / "track.yml"
    backup = album / "track.yml.bak"
    source.write_text("current")
    backup.write_text("original")

    def fail_replace(source: Path, destination: Path) -> None:
        raise PermissionError("replacement denied")

    monkeypatch.setattr(populate.os, "replace", fail_replace)
    monkeypatch.setattr(sys, "argv", ["script", "--revert", "--dir", str(album)])
    assert populate.main() == 1
    assert "replacement denied" in caplog.text
    assert source.read_text() == "current"
    assert backup.read_text() == "original"


def test_clean_and_revert_are_mutually_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["script", "--clean", "--revert"])
    with pytest.raises(SystemExit) as exception:
        populate.main()
    assert exception.value.code == 2


def test_explicit_file_requires_album_directory_and_backup(tmp_path: Path, album: Path) -> None:
    outside = tmp_path / "track.yml"
    with pytest.raises(ValueError, match="album directory"):
        populate.revert(specific_file=outside)
    with pytest.raises(ValueError, match="backup does not exist"):
        populate.revert(specific_file=album / "missing.yml")
