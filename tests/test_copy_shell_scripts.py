import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


SHELL_DIR = Path(__file__).parents[1] / "src" / "mediascripts" / "shell"
SCRIPTS = sorted(SHELL_DIR.glob("copy_*.sh"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
@pytest.mark.parametrize("keywords", ["First Artist\nSecond Artist", ""])
def test_copy_scripts_forward_exclusions(
    tmp_path: Path, script: Path, keywords: str,
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
    assert args[index + 1:index + 1 + len(keywords.split("\n"))] == keywords.split("\n")
    assert args[index + 1 + len(keywords.split("\n"))] == "--diff"
    assert args[-1] == "--dry-run"
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
    tmp_path: Path, script: str, filename: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "copy-medialib"
    command.write_text(
        f'#!/bin/bash\nexec {shlex.quote(sys.executable)} -m mediascripts.copy.copy_medialib "$@"\n',
        encoding="utf-8",
    )
    command.chmod(0o755)
    source = "demo" if "_from_demo_to_local" in script else "local"
    destination = "local" if source == "demo" else "demo"
    for artist in ("Excluded Artist", "Other Blocked", "Included Artist"):
        track = tmp_path / source / "data" / "Music" / artist / "Album" / filename
        track.parent.mkdir(parents=True)
        track.write_text("test data", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MOONGAS_COLLECTION_LOCAL": str(tmp_path / "local"),
        "MOONGAS_COLLECTION_DEMO": str(tmp_path / "demo"),
        "COPY_MEDIALIB_EXCLUDE_KEYWORDS": "Excluded Artist\nOther Blocked",
    }

    subprocess.run(["bash", str(SHELL_DIR / script)], env=env, capture_output=True, check=True)
    destination_music = tmp_path / destination / "data" / "Music"
    assert (destination_music / "Included Artist" / "Album" / filename).read_text() == "test data"
    assert not (destination_music / "Excluded Artist" / "Album" / filename).exists()
    assert not (destination_music / "Other Blocked" / "Album" / filename).exists()
