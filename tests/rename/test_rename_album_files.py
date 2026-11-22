import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from ruamel.yaml.error import YAMLError
from yaml import safe_load

from mediascripts.rename import rename_album_files as rename
from mediascripts.rename.rename_album_files import update_filename


def test_update_filename_applies_filename_cleanup_rules() -> None:
    filename = "03 - Lil Wayne - MegaMan [Official Music Video] 🔄.mp3"

    assert update_filename(filename, "Lil Wayne") == "03 - MegaMan.mp3"


def test_update_filename_normalizes_artist_and_featured_artist() -> None:
    filename = "04 - Jay‐Z feat. Rick Ross - FuckWithMeYouKnowIGotIt.mp3"

    assert (
        update_filename(filename, "Jay-Z")
        == "04 - Jay-Z ft Rick Ross - FuckWithMeYouKnowIGotIt.mp3"
    )


@pytest.mark.parametrize(
    "filename, artist, expected",
    [
        ("01.02 - I Found Love.yml", "X", "01.02 - I Found Love.yml"),
        ("01.02 - I Found Love.flac", "X", "01.02 - I Found Love.flac"),
        ("U2 - I Still Haven't Found.lrc", "U2", "I Still Haven't Found.lrc"),
        (
            "Men I Trust - Oncle Jazz - 05 Found Me.yml",
            "Men I Trust",
            "Oncle Jazz - 05 Found Me.yml",
        ),
        ("A & B – Song.lrc", "A and B", "Song.lrc"),
        ("01 - Song (Official Video).mp3", "X", "01 - Song.mp3"),
        ("01 - Song (Official HD Video).mp3", "X", "01 - Song.mp3"),
        ("01 - Song (official lyric video).mp3", "X", "01 - Song.mp3"),
        ("01 - Song (Audio).mp3", "X", "01 - Song.mp3"),
        ("01 - Song (Remastered).mp3", "X", "01 - Song (Remastered).mp3"),
        ("01 - Song Feat. A.mp3", "X", "01 - Song ft A.mp3"),
        ("01 - Song (FEAT A).mp3", "X", "01 - Song (ft A).mp3"),
        ("01 - Song Ft_ A.mp3", "X", "01 - Song ft A.mp3"),
        ("01 - Song featuring A.mp3", "X", "01 - Song ft A.mp3"),
        ("01 - Daft Creature Defeat.mp3", "X", "01 - Daft Creature Defeat.mp3"),
        ("05 - Nuthin’ but a “G” Thang.yml", "Dr_ Dre", "05 - Nuthin' but a 'G' Thang.yml"),
        ('Song "quoted".mp3', "X", "Song 'quoted'.mp3"),
    ],
)
def test_update_filename_cases(filename: str, artist: str, expected: str) -> None:
    assert update_filename(filename, artist) == expected


def test_update_filename_renames_sidecars_consistently() -> None:
    stems = {
        os.path.splitext(
            update_filename(f"03 - Lil Wayne - MegaMan (feat. X){ext}", "Lil Wayne")
        )[0]
        for ext in (".mp3", ".flac", ".yml", ".lrc", ".txt")
    }
    assert stems == {"03 - MegaMan (ft X)"}


@pytest.mark.parametrize(
    ("flags", "process_collection", "dry_run"),
    [
        (["-q"], False, False),
        (["--quiet"], True, False),
        (["--quiet", "--dry-run"], False, True),
    ],
)
def test_quiet_cli_only_prints_actual_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    flags: list[str], process_collection: bool, dry_run: bool,
) -> None:
    album = tmp_path / "data" / "Music" / "Artist" / "Album [2000]"
    album.mkdir(parents=True)
    original = album / "Song (Official Video).mp3"
    original.touch()
    (album / "Other (Official Video).mp3").touch()
    (album / "Other.mp3").write_text("keep")
    args = ["rename-album-files", *flags]
    if process_collection:
        monkeypatch.setattr(rename, "MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    else:
        args += ["--dir", str(album)]
    monkeypatch.setattr(sys, "argv", args)

    rename.main()

    captured = capsys.readouterr()
    assert captured.out == ("" if dry_run else 'Renamed "Song (Official Video).mp3" -> "Song.mp3"\n')
    assert not captured.err
    assert original.exists() == dry_run
    assert (album / "Song.mp3").exists() != dry_run
    assert (album / "Other (Official Video).mp3").exists()
    assert (album / "Other.mp3").read_text() == "keep"


def test_quiet_cli_still_reports_invalid_album_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "Missing [2000]"
    monkeypatch.setattr(sys, "argv", ["rename-album-files", "-q", "--dir", str(missing)])

    rename.main()

    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == f"Album path is invalid, rename operation aborted: {missing}\n"


@pytest.mark.parametrize("yaml_extension", [".yml", ".yaml"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_rename_updates_all_media_references(
    tmp_path: Path, yaml_extension: str, dry_run: bool,
) -> None:
    album = tmp_path / "data" / "Music" / "Artist" / "Album [2000]"
    album.mkdir(parents=True)
    stem = "Song [Official Video]"
    for extension in (".mp3", ".flac", ".mp4"):
        (album / f"{stem}{extension}").write_bytes(b"media")
    original_yaml = album / f"{stem}{yaml_extension}"
    content = (
        "# Keep this comment\n"
        "trackData:\n  title: Song\n"
        "mediaFileData:\n"
        f'  - path: "/data/Music/Artist/Album [2000]/{stem}.mp3"\n'
        f"  - path: '{stem}.flac'\n"
        f"  - path: ./{stem}.mp4\n"
        f"  - path: Missing [Official Video].mp3\n"
        f"otherReferences: ['{stem}.mp3', '{stem}.flac']\n"
    )
    original_yaml.write_text(content, encoding="utf-8")
    original_yaml.chmod(0o640)

    assert rename.rename_album_files(album, dry_run=dry_run, quiet=True) == 4

    if dry_run:
        assert original_yaml.read_text(encoding="utf-8") == content
        assert sorted(path.name for path in album.iterdir()) == sorted(
            [f"{stem}{ext}" for ext in (".mp3", ".flac", ".mp4", yaml_extension)]
        )
        return
    updated_yaml = album / f"Song{yaml_extension}"
    document = safe_load(updated_yaml.read_text(encoding="utf-8"))
    assert [entry["path"] for entry in document["mediaFileData"]] == [
        "/data/Music/Artist/Album [2000]/Song.mp3",
        "Song.flac",
        "./Song.mp4",
        "Missing [Official Video].mp3",
    ]
    assert document["otherReferences"] == ["Song.mp3", "Song.flac"]
    assert document["trackData"]["title"] == "Song"
    assert "# Keep this comment" in updated_yaml.read_text(encoding="utf-8")
    assert updated_yaml.stat().st_mode & 0o777 == 0o640
    assert not original_yaml.exists()
    for extension in (".mp3", ".flac", ".mp4"):
        assert (album / f"Song{extension}").read_bytes() == b"media"


def test_rename_updates_unchanged_yaml_and_preserves_collision_references(tmp_path: Path) -> None:
    album = tmp_path / "data" / "Music" / "Artist" / "Album [2000]"
    album.mkdir(parents=True)
    (album / "Song [Official Video].mp3").touch()
    (album / "Song.mp3").write_bytes(b"keep")
    (album / "Song [Official Video].flac").touch()
    yaml_path = album / "Song.yml"
    yaml_path.write_text(
        "mediaFileData:\n"
        "  - path: Song [Official Video].mp3\n"
        "  - path: Song [Official Video].flac\n",
        encoding="utf-8",
    )

    assert rename.rename_album_files(album, quiet=True) == 1

    assert safe_load(yaml_path.read_text(encoding="utf-8")) == {
        "mediaFileData": [
            {"path": "Song [Official Video].mp3"},
            {"path": "Song.flac"},
        ]
    }
    assert (album / "Song.mp3").read_bytes() == b"keep"


def test_rename_preserves_yaml_validity_when_normalizing_apostrophes(tmp_path: Path) -> None:
    album = tmp_path / "data" / "Music" / "Artist" / "Album [2000]"
    album.mkdir(parents=True)
    (album / "Song’s [Official Video].mp3").touch()
    yaml_path = album / "Track.yml"
    yaml_path.write_text(
        "mediaFileData:\n"
        "  - path: 'Song’s [Official Video].mp3'\n"
        '  - path: "Song’s [Official Video].mp3"\n'
        "unrelated: Song’s [Official Video].mp3.bak\n",
        encoding="utf-8",
    )

    assert rename.rename_album_files(album, quiet=True) == 1

    assert safe_load(yaml_path.read_text(encoding="utf-8")) == {
        "mediaFileData": [{"path": "Song's.mp3"}, {"path": "Song's.mp3"}],
        "unrelated": "Song’s [Official Video].mp3.bak",
    }
    assert (album / "Song's.mp3").exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_rename_double_quotes_for_ntfs_and_update_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool,
) -> None:
    album = tmp_path / "data" / "Music" / "Dr_ Dre" / "The Chronic [1992]"
    album.mkdir(parents=True)
    original_stem = "05 - Nuthin’ but a “G” Thang"
    updated_stem = "05 - Nuthin' but a 'G' Thang"
    (album / f"{original_stem}.mp3").write_bytes(b"media")
    yaml_path = album / f"{original_stem}.yml"
    original_content = f'mediaFileData:\n  - path: "{original_stem}.mp3"\n'
    yaml_path.write_text(original_content, encoding="utf-8")
    real_rename = rename.os.rename

    def ntfs_rename(source: Path, destination: Path) -> None:
        if '"' in destination.name:
            raise OSError(22, "Invalid argument", str(destination))
        real_rename(source, destination)

    monkeypatch.setattr(rename.os, "rename", ntfs_rename)

    assert rename.rename_album_files(album, dry_run=dry_run, quiet=True) == 2
    if dry_run:
        assert yaml_path.read_text(encoding="utf-8") == original_content
        assert (album / f"{original_stem}.mp3").exists()
    else:
        assert (album / f"{updated_stem}.mp3").read_bytes() == b"media"
        assert safe_load((album / f"{updated_stem}.yml").read_text(encoding="utf-8")) == {
            "mediaFileData": [{"path": f"{updated_stem}.mp3"}],
        }
        assert not yaml_path.exists()


def test_quote_normalization_collision_preserves_references(tmp_path: Path) -> None:
    album = tmp_path / "data" / "Music" / "Artist" / "Album [2000]"
    album.mkdir(parents=True)
    original = album / "Song “quoted”.mp3"
    original.write_bytes(b"original")
    target = album / "Song 'quoted'.mp3"
    target.write_bytes(b"existing")
    yaml_path = album / "Track.yml"
    content = 'mediaFileData:\n  - path: Song “quoted”.mp3\n'
    yaml_path.write_text(content, encoding="utf-8")

    assert rename.rename_album_files(album, quiet=True) == 0
    assert original.read_bytes() == b"original"
    assert target.read_bytes() == b"existing"
    assert yaml_path.read_text(encoding="utf-8") == content


def test_invalid_yaml_aborts_before_renaming(tmp_path: Path) -> None:
    album = tmp_path / "data" / "Music" / "Artist" / "Album [2000]"
    album.mkdir(parents=True)
    media_path = album / "Song [Official Video].mp3"
    media_path.touch()
    yaml_path = media_path.with_suffix(".yml")
    yaml_path.write_text("mediaFileData: [\n", encoding="utf-8")

    with pytest.raises(YAMLError):
        rename.rename_album_files(album, quiet=True)

    assert media_path.exists()
    assert yaml_path.read_text(encoding="utf-8") == "mediaFileData: [\n"
    assert len(list(album.iterdir())) == 2


@pytest.mark.parametrize("failure", ["rename", "save"])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_rename_rolls_back_on_filesystem_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
    error_type: type[OSError] | type[KeyboardInterrupt],
) -> None:
    album = tmp_path / "data" / "Music" / "Artist" / "Album [2000]"
    album.mkdir(parents=True)
    (album / "Song [Official Video].mp3").write_bytes(b"media")
    content = "mediaFileData:\n  - path: Song [Official Video].mp3\n"
    (album / "Song [Official Video].yml").write_text(content, encoding="utf-8")
    (album / "Other.yml").write_text(content, encoding="utf-8")
    originals = {path.name: path.read_bytes() for path in album.iterdir()}
    calls = 0
    if failure == "rename":
        original_rename = rename.os.rename

        def fail_second_rename(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise error_type("rename failed")
            original_rename(source, destination)

        monkeypatch.setattr(rename.os, "rename", fail_second_rename)
    else:
        original_save = rename.save_yaml_atomically

        def fail_second_save(path: Path, write: Callable[[rename.TextWriter], None]) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise error_type("save failed")
            original_save(path, write)

        monkeypatch.setattr(rename, "save_yaml_atomically", fail_second_save)

    with pytest.raises(error_type, match=f"{failure} failed"):
        rename.rename_album_files(album, quiet=True)

    assert {path.name: path.read_bytes() for path in album.iterdir()} == originals
