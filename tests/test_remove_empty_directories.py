import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "src/mediascripts/shell/remove_empty_directories.sh"


def run_script(root: Path | None, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("MOONGAS_COLLECTION_ROOTDIR", None)
    if root is not None:
        env["MOONGAS_COLLECTION_ROOTDIR"] = str(root)
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=cwd, env=env, capture_output=True, text=True,
    )


def test_removes_nested_empty_directories_but_preserves_files_and_data_root(tmp_path: Path) -> None:
    root = tmp_path / "collection with spaces"
    data = root / "data"
    empty = data / "Artist" / "Album [2000]" / "nested"
    empty.mkdir(parents=True)
    occupied = data / "Other Artist" / "Album"
    occupied.mkdir(parents=True)
    hidden = occupied / ".keep"
    hidden.touch()
    outside = root / "outside data"
    outside.mkdir()

    result = run_script(root, tmp_path)

    assert result.returncode == 0, result.stderr
    assert not (data / "Artist").exists()
    assert hidden.is_file()
    assert data.is_dir()
    assert outside.is_dir()
    assert str(empty) in result.stdout
    hidden.unlink()
    assert run_script(root, tmp_path).returncode == 0
    assert data.is_dir() and not list(data.iterdir())
    assert run_script(root, tmp_path).returncode == 0


def test_follows_library_and_data_links_but_keeps_links(tmp_path: Path) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    data = tmp_path / "actual data"
    data.mkdir()
    (root / "data").symlink_to(data, target_is_directory=True)
    library = tmp_path / "library"
    (library / "Artist" / "Album").mkdir(parents=True)
    link = data / "Music"
    link.symlink_to(library, target_is_directory=True)
    dangling = data / "dangling"
    dangling.symlink_to(tmp_path / "missing")

    result = run_script(root, tmp_path)

    assert result.returncode == 0, result.stderr
    assert not (library / "Artist").exists()
    assert library.is_dir()
    assert link.is_symlink()
    assert dangling.is_symlink()
    assert (root / "data").is_symlink()
    assert data.is_dir()


@pytest.mark.parametrize("configured", [False, True])
def test_invalid_root_fails_without_falling_back_to_cwd(tmp_path: Path, configured: bool) -> None:
    empty = tmp_path / "keep"
    empty.mkdir()
    result = run_script(tmp_path if configured else None, tmp_path)
    assert result.returncode != 0
    assert "must be set" in result.stderr or "does not exist" in result.stderr
    assert empty.is_dir()


def test_refuses_filesystem_root_as_data(tmp_path: Path) -> None:
    (tmp_path / "data").symlink_to("/", target_is_directory=True)
    result = run_script(tmp_path, tmp_path)
    assert result.returncode != 0
    assert "Refusing" in result.stderr
