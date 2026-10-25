import argparse
import logging
import sys
from pathlib import Path

import pytest
from mediascan.types.media_file.media_file_data import MediaFileData
from mediascan.types.media_files.media_files_yaml_file import MediaFilesYamlFile

from mediascripts.artist.artist_yaml import (
    artist_yaml_auto_populate as populate,
    artist_yaml_common as common,
    artist_yaml_reformat_with_files_yaml as reformat,
    artist_yaml_test_with_files_yaml as validate,
)


@pytest.fixture
def files_yaml(tmp_path: Path) -> Path:
    tracks = [
        ("Artist", "First [2000]", "01.mp3"),
        ("Artist", "Second [2001]", "02.mp3"),
        ("Other", "Album [2002]", "03.mp3"),
    ]
    files = [
        MediaFileData(
            path=str(tmp_path / artist / album / filename),
            size=1,
            format="mp3",
            title=filename,
            artist=artist,
            albumartist=artist,
            album=album,
            genre="Rock",
            year=2000,
            duration=1,
        )
        for artist, album, filename in tracks
    ]
    path = tmp_path / "files.yml"
    path.write_text(MediaFilesYamlFile(files=files).to_yaml(), encoding="utf-8")
    return path


def test_load_artist_paths_preserves_first_occurrence_and_order(
    files_yaml: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert common.load_artist_paths(files_yaml) == {
        "Artist": files_yaml.parent / "Artist",
        "Other": files_yaml.parent / "Other",
    }
    assert capsys.readouterr().out == "loaded 2 artist paths from mediafiles yaml\n"


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
    assert reformat.excluded is validate.excluded is common.excluded


def test_shared_files_yaml_argument() -> None:
    parser = argparse.ArgumentParser()
    common.add_files_yaml_argument(parser)
    assert parser.parse_args([]).files_yaml_path == "../../out/files.yml"
    assert parser.parse_args(["--files-yaml-path", "custom.yml"]).files_yaml_path == "custom.yml"


@pytest.mark.parametrize("module,label", [(reformat, "Missing count"), (validate, "Missing/Invalid count")])
def test_scripts_share_path_loading_and_keep_reporting(
    files_yaml: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module: object,
    label: str,
) -> None:
    assert module in (reformat, validate)
    script = reformat if module is reformat else validate
    assert script.load_artist_paths is common.load_artist_paths
    monkeypatch.chdir(tmp_path)
    arguments = ["script", "--files-yaml-path", str(files_yaml)]
    if script is validate:
        arguments += ["--source-path", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", arguments)
    script.main()
    output = capsys.readouterr().out
    assert "loaded 2 artist paths from mediafiles yaml" in output
    assert "Found artist.yml for 0 out of 2 artist folders" in output
    assert f"{label}: 2" in output


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


def test_duplicate_members_cleanup_is_shared(tmp_path: Path) -> None:
    content = "artistData:\n  members:\n    - name: First\n  members:\n    - name: Second\n"
    expected = "artistData:\n  members:\n    - name: First\n    - name: Second\n"
    path = tmp_path / "artist.yml"
    path.write_text(content, encoding="utf-8")
    assert common.remove_duplicate_members_lists(content) == expected
    assert common.remove_duplicate_members_lists_from_collection(tmp_path) == 1
    assert path.read_text() == expected
    assert common.remove_duplicate_members_lists_from_collection(tmp_path) == 0
    assert populate.remove_duplicate_members_lists is common.remove_duplicate_members_lists


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
