import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


SHELL_DIR = Path(__file__).parents[1] / "src" / "mediascripts" / "shell"
SCRIPTS = sorted(SHELL_DIR.glob("copy_*.sh"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
@pytest.mark.parametrize(
    "keywords, expected",
    [("", []), ("\nFirst Artist\n\nSecond Artist\n", ["First Artist", "Second Artist"])],
)
def test_copy_scripts_forward_exclusions(
    tmp_path: Path, script: Path, keywords: str, expected: list[str],
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "copy-medialib"
    command.write_text('#!/bin/bash\nprintf "%s\\0" "$@"\n', encoding="utf-8")
    command.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MOONGAS_COLLECTION_LOCAL": str(tmp_path / "local"),
        "MOONGAS_COLLECTION_DEMO": str(tmp_path / "demo"),
        "COPY_MEDIALIB_EXCLUDE_KEYWORDS": keywords,
    }

    result = subprocess.run(
        ["bash", str(script), "--dry-run"], env=env, capture_output=True, check=True,
    )
    args = result.stdout.decode().split("\0")[:-1]
    index = args.index("--exclude-keywords")
    assert args[index + 1:] == [*expected, "--diff", "--dry-run"]
    source = "demo" if "_from_demo_to_local" in script.name else "local"
    destination = "local" if source == "demo" else "demo"
    assert args[args.index("--src-paths") + 1] == str(tmp_path / source / "data" / "Music")
    assert args[args.index("--dst-path") + 1] == f"{tmp_path / destination / 'data'}/"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_copy_scripts_require_exclusion_variable(tmp_path: Path, script: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key != "COPY_MEDIALIB_EXCLUDE_KEYWORDS"}
    env["MOONGAS_COLLECTION_LOCAL"] = str(tmp_path / "local")
    env["MOONGAS_COLLECTION_DEMO"] = str(tmp_path / "demo")
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "COPY_MEDIALIB_EXCLUDE_KEYWORDS: unbound variable" in result.stderr


@pytest.fixture
def copy_command_path(tmp_path: Path) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "copy-medialib"
    command.write_text(
        f'#!/bin/bash\nexec {shlex.quote(sys.executable)} -m mediascripts.copy.copy_medialib "$@"\n',
        encoding="utf-8",
    )
    command.chmod(0o755)
    return f"{bin_dir}:{os.environ['PATH']}"


@pytest.mark.parametrize(
    "script, filename",
    [
        ("copy_lyrics_files_from_demo_to_local.sh", "song.txt"),
        ("copy_lyrics_files_from_local_to_demo.sh", "song.lrc"),
        ("copy_media_files_from_demo_to_local.sh", "song.mp3"),
        ("copy_media_files_from_local_to_demo.sh", "song.m4a"),
    ],
)
def test_copy_scripts_exclude_matching_files(
    tmp_path: Path, copy_command_path: str, script: str, filename: str,
) -> None:
    source = "demo" if "_from_demo_to_local" in script else "local"
    destination = "local" if source == "demo" else "demo"
    for artist in ("Excluded Artist", "Other Blocked", "Included Artist"):
        track = tmp_path / source / "data" / "Music" / artist / "Album" / filename
        track.parent.mkdir(parents=True)
        track.write_text("test data", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": copy_command_path,
        "MOONGAS_COLLECTION_LOCAL": str(tmp_path / "local"),
        "MOONGAS_COLLECTION_DEMO": str(tmp_path / "demo"),
        "COPY_MEDIALIB_EXCLUDE_KEYWORDS": "Excluded Artist\nOther Blocked",
    }

    subprocess.run(["bash", str(SHELL_DIR / script)], env=env, capture_output=True, check=True)
    destination_music = tmp_path / destination / "data" / "Music"
    assert (destination_music / "Included Artist" / "Album" / filename).read_text() == "test data"
    assert not (destination_music / "Excluded Artist" / "Album" / filename).exists()
    assert not (destination_music / "Other Blocked" / "Album" / filename).exists()


@pytest.mark.parametrize("keywords", ["", "\nExcluded Artist\n\n"])
def test_copy_all_yaml_wrapper_deletes_only_unexcluded_orphans(
    tmp_path: Path, copy_command_path: str, keywords: str,
) -> None:
    source = tmp_path / "local" / "data" / "Music"
    destination = tmp_path / "demo" / "data" / "Music"
    album = Path("The Strokes") / "Room On Fire [2003]"
    (source / album).mkdir(parents=True)
    (source / album / "keep.yml").touch()
    (destination / album).mkdir(parents=True)
    orphan = destination / album / "11 Take It Or Leave It.yml"
    keep = destination / album / "keep.yml"
    unmatched = destination / album / "notes.txt"
    excluded = destination / "Excluded Artist" / "extra.yml"
    excluded.parent.mkdir()
    for path in (orphan, keep, unmatched, excluded):
        path.touch()
    env = {
        **os.environ,
        "PATH": copy_command_path,
        "MOONGAS_COLLECTION_LOCAL": str(tmp_path / "local"),
        "MOONGAS_COLLECTION_DEMO": str(tmp_path / "demo"),
        "COPY_MEDIALIB_EXCLUDE_KEYWORDS": keywords,
    }
    command = ["bash", str(SHELL_DIR / "copy_all_yaml_files_from_local_to_demo.sh"), "--delete", "-q"]

    preview = subprocess.run(
        [*command, "--dry-run"], env=env, capture_output=True, text=True, check=True,
    )
    assert f"Would delete: {orphan}" in preview.stderr
    assert orphan.exists()

    subprocess.run(command, env=env, capture_output=True, check=True)
    assert not orphan.exists()
    assert keep.exists()
    assert unmatched.exists()
    assert excluded.exists() == bool(keywords)
