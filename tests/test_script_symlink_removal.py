import os
import subprocess
import sys
from pathlib import Path

import pytest

from mediascripts import main as scripts_main


ROOT = Path(__file__).resolve().parents[1]


def test_remove_script_symlinks_only_removes_unprotected_file_links(tmp_path: Path) -> None:
    collection = tmp_path / "collection"
    collection.mkdir()
    targets = tmp_path / "targets"
    targets.mkdir()
    target_file = targets / "file"
    target_file.write_text("unchanged", encoding="utf-8")
    target_dir = targets / "directory"
    target_dir.mkdir()
    nested_link = target_dir / "nested"
    nested_link.symlink_to(target_file)
    (collection / "file-link").symlink_to(target_file)
    intermediate = targets / "intermediate"
    intermediate.symlink_to(target_file)
    (collection / "file-link-chain").symlink_to(intermediate)
    (collection / "directory-link").symlink_to(target_dir, target_is_directory=True)
    (collection / "dangling").symlink_to(targets / "missing")
    (collection / ".hidden").symlink_to(target_file)
    (collection / "data").symlink_to(target_file)
    (collection / ".venv").symlink_to(target_file)
    fifo = targets / "fifo"
    os.mkfifo(fifo)
    (collection / "fifo-link").symlink_to(fifo)
    regular_file = collection / "regular"
    regular_file.write_text("keep", encoding="utf-8")
    regular_dir = collection / "nested"
    regular_dir.mkdir()
    inner_link = regular_dir / "link"
    inner_link.symlink_to(target_file)

    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "mediascripts", "--remove-script-symlinks"],
        cwd=collection, env=env, capture_output=True, text=True, check=True,
    )

    for name in ("file-link", "file-link-chain", ".hidden"):
        assert not (collection / name).is_symlink()
        assert f"removed symlink {name}" in result.stderr
    for name in ("directory-link", "dangling", "fifo-link"):
        assert (collection / name).is_symlink()
    assert (collection / "data").is_symlink()
    assert (collection / ".venv").is_symlink()
    assert regular_file.read_text(encoding="utf-8") == "keep"
    assert regular_dir.is_dir() and inner_link.is_symlink()
    assert target_file.read_text(encoding="utf-8") == "unchanged"
    assert target_dir.is_dir() and nested_link.is_symlink()
    assert intermediate.is_symlink()


@pytest.mark.parametrize("other", ["--list", "--create-script-symlinks"])
def test_removal_rejects_conflicting_options(tmp_path: Path, other: str) -> None:
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "missing")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "mediascripts", "--remove-script-symlinks", other],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "cannot be combined" in result.stderr
    assert link.is_symlink()


def test_removal_propagates_unlink_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "file"
    target.touch()
    (tmp_path / "link").symlink_to(target)

    def fail(_path: Path, missing_ok: bool = False) -> None:
        raise PermissionError("cannot remove symlink")

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(PermissionError, match="cannot remove symlink"):
        scripts_main.remove_script_symlinks(tmp_path)


def test_shell_manifest_matches_source_scripts() -> None:
    shell_dir = ROOT / "src" / "mediascripts" / "shell"
    names = (shell_dir / "scripts.txt").read_text(encoding="utf-8").splitlines()
    assert len(names) == len(set(names))
    assert set(names) == {path.name for path in shell_dir.glob("*.sh")}


def test_create_script_symlinks_ignores_stale_installed_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_dir = tmp_path / "package"
    shell_dir = package_dir / "shell"
    shell_dir.mkdir(parents=True)
    (shell_dir / "scripts.txt").write_text("copy_album_yaml_files.sh\n", encoding="utf-8")
    current = shell_dir / "copy_album_yaml_files.sh"
    current.write_text("#!/bin/bash\n", encoding="utf-8")
    for name in (
        "copy_album_yaml_files_from_demo_to_local.sh",
        "copy_album_yaml_files_from_local_to_demo.sh",
        "scan_to_artists_yaml.sh",
    ):
        (shell_dir / name).write_text("#!/bin/bash\n", encoding="utf-8")
    destination = tmp_path / "collection"
    destination.mkdir()
    monkeypatch.setattr(scripts_main, "__file__", str(package_dir / "main.py"))
    monkeypatch.setattr(scripts_main.importlib.metadata, "entry_points", lambda **_: [])

    scripts_main.create_script_symlinks(destination)

    assert (destination / "copy-album-yaml-files").resolve() == current.resolve()
    assert sorted(path.name for path in destination.iterdir()) == ["copy-album-yaml-files"]


def test_create_script_symlinks_reports_missing_manifest_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_dir = tmp_path / "package"
    shell_dir = package_dir / "shell"
    shell_dir.mkdir(parents=True)
    (shell_dir / "scripts.txt").write_text("missing.sh\n", encoding="utf-8")
    monkeypatch.setattr(scripts_main, "__file__", str(package_dir / "main.py"))
    monkeypatch.setattr(scripts_main.importlib.metadata, "entry_points", lambda **_: [])
    with pytest.raises(FileNotFoundError, match="missing.sh"):
        scripts_main.create_script_symlinks(tmp_path)
