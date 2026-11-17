from io import StringIO
from pathlib import Path

import pytest
from yaml import safe_load

from mediascripts.track.track_yaml import track_yaml_common as common


@pytest.mark.parametrize("value", sorted(common.BOOLEAN_LIKE_STRINGS))
def test_track_yaml_quotes_boolean_like_strings_for_yaml_11_consumers(value: str) -> None:
    yaml = common.track_yaml()
    document = {"trackData": {"title": value}}

    stream = StringIO()
    yaml.dump(document, stream)

    content = stream.getvalue()
    assert f'title: "{value}"' in content
    assert safe_load(content) == document


def test_track_yaml_round_trip_preserves_quoted_boolean_like_string() -> None:
    yaml = common.track_yaml()
    document = yaml.load('trackData:\n  title: "true"\n')

    stream = StringIO()
    yaml.dump(document, stream)

    assert 'title: "true"' in stream.getvalue()


@pytest.mark.parametrize("typ", ["rt", "safe"])
@pytest.mark.parametrize("value", [
    "Unfinished Music No. 1: Two Virgins", "Song #1", "[Untitled]", "*Star",
    "&Artist", "!Title", "A \"quote\": B", "C:\\Music\\Track", "Line one\nLine two",
])
def test_track_yaml_preserves_yaml_punctuation(typ: str, value: str) -> None:
    document = {"trackData": {"title": value, "year": 1968}}
    stream = StringIO()

    common.track_yaml(typ=typ).dump(document, stream)

    assert safe_load(stream.getvalue()) == document


@pytest.mark.parametrize("backup", [False, True])
def test_atomic_save_preserves_permissions_and_optional_backup(tmp_path: Path, backup: bool) -> None:
    path = tmp_path / "track.yml"
    path.write_text("original")
    path.chmod(0o640)
    backup_path = tmp_path / "track.yml.bak" if backup else None

    def write(stream: common.TextWriter) -> None:
        stream.write("updated")

    common.save_yaml_atomically(path, write, backup_path)
    assert path.read_text() == "updated"
    assert path.stat().st_mode & 0o777 == 0o640
    if backup_path is not None:
        assert backup_path.read_text() == "original"
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_save_serialization_failure_leaves_original_and_backup_untouched(tmp_path: Path) -> None:
    path = tmp_path / "track.yml"
    path.write_text("original")
    backup = tmp_path / "track.yml.bak"
    backup.write_text("earlier backup")

    def write(stream: common.TextWriter) -> None:
        stream.write("partial")
        raise ValueError("serialization failed")

    with pytest.raises(ValueError, match="serialization failed"):
        common.save_yaml_atomically(path, write, backup)
    assert path.read_text() == "original"
    assert backup.read_text() == "earlier backup"
    assert not list(tmp_path.glob(".*.tmp"))


def test_shared_walk_propagates_errors_unless_handler_provided(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        list(common.iter_collection_files(missing))
    errors: list[OSError] = []
    assert list(common.iter_collection_files(missing, onerror=errors.append)) == []
    assert len(errors) == 1


def test_shared_diff_format_preserves_merge_layout() -> None:
    assert common.format_field_diff("title", "'Old'", "['New']", "Media File Tags") == (
        "  title: Track YAML = 'Old'; Media File Tags = ['New']"
    )
