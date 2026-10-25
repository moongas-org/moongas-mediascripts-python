from pathlib import Path

import pytest

from mediascripts.track.track_yaml import track_yaml_common as common


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
