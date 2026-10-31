import argparse
import sqlite3
import sys
from pathlib import Path

import pytest

from mediascripts.artist.artist_yaml.artist_yaml_reformat import main as reformat_main
from mediascripts.artist.artist_yaml.artist_yaml_test import main as validate_main
from mediascripts.artist.artist_yaml.artist_yaml_common import load_artist_paths_from_database


VALID_ARTIST_YAML = """\
artistData:
  artistNames:
    - Example Artist
  city: London
  countryCode: GB
  regionCode: GB-SRY
  languageCodes:
    - en
"""


@pytest.fixture
def collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "collection"
    root.mkdir()
    artist_path = tmp_path / "Music" / "Example Artist"
    artist_path.mkdir(parents=True)
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(root))
    with sqlite3.connect(root / "mediascan.db") as connection:
        connection.execute("CREATE TABLE artist (id INTEGER PRIMARY KEY, path TEXT, name TEXT)")
        connection.execute("INSERT INTO artist (path, name) VALUES (?, ?)", (str(artist_path), "Example Artist"))
    return root, artist_path


def test_load_artist_paths_from_database(collection: tuple[Path, Path]) -> None:
    _, artist_path = collection
    parser = argparse.ArgumentParser()

    assert load_artist_paths_from_database(parser) == {"Example Artist": artist_path}


def test_reformat_uses_database_paths_and_updates_artist_yaml(
    collection: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, artist_path = collection
    artist_yaml = artist_path / "artist.yml"
    artist_yaml.write_text(VALID_ARTIST_YAML, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["artist-yaml-reformat"])

    reformat_main()

    assert "regionCode: GB-LND" in artist_yaml.read_text(encoding="utf-8")
    assert f"Updated {artist_yaml}" in capsys.readouterr().out


def test_reformat_all_rewrites_database_artists(
    collection: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, artist_path = collection
    artist_yaml = artist_path / "artist.yml"
    artist_yaml.write_text(VALID_ARTIST_YAML, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["artist-yaml-reformat", "--reformat-all"])

    reformat_main()

    assert "artistData:" in artist_yaml.read_text(encoding="utf-8")


def test_validator_uses_database_paths_and_reports_orphans(
    collection: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, artist_path = collection
    (artist_path / "artist.yml").write_text(VALID_ARTIST_YAML, encoding="utf-8")
    orphan_path = tmp_path / "Music" / "Orphan" / "artist.yml"
    orphan_path.parent.mkdir()
    orphan_path.write_text(VALID_ARTIST_YAML, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["artist-yaml-test", "--source-path", str(tmp_path / "Music")])
    monkeypatch.chdir(tmp_path)

    validate_main()

    output = capsys.readouterr().out
    assert "Found artist.yml for 1 out of 1 artist folders" in output
    assert "not in any artist path" in output
    assert str(orphan_path) in output
    assert not (tmp_path / "artists_missing.txt").exists()


def test_reformat_requires_collection_root(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("MOONGAS_COLLECTION_ROOTDIR", raising=False)
    monkeypatch.setattr(sys, "argv", ["artist-yaml-reformat"])

    with pytest.raises(SystemExit) as error:
        reformat_main()

    assert error.value.code == 2
    assert "MOONGAS_COLLECTION_ROOTDIR must be set" in capsys.readouterr().err
