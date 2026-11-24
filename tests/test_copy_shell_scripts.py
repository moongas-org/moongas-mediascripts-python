import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from mediascripts import main as scripts_main


SHELL_DIR = Path(__file__).parents[1] / "src" / "mediascripts" / "shell"
SCRIPTS = sorted(SHELL_DIR.glob("copy_*.sh"))
SCRIPT_NAMES = {
    f"copy_{kind}_files.sh"
    for kind in ("album_yaml", "all_yaml", "artist_yaml", "lyrics", "media")
}


def test_expected_copy_scripts_are_installed() -> None:
    assert {script.name for script in SCRIPTS} == SCRIPT_NAMES
    assert all(os.access(script, os.X_OK) for script in SCRIPTS)


def test_create_script_symlinks_uses_new_command_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scripts_main.importlib.metadata, "entry_points", lambda **_: [])
    scripts_main.create_script_symlinks(tmp_path)
    for script in SCRIPTS:
        link = tmp_path / script.stem.replace("_", "-")
        assert link.is_symlink()
        assert link.resolve() == script.resolve()
        assert os.access(link, os.X_OK)


def test_generated_symlink_executes_with_shared_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "copy-medialib"
    command.write_text('#!/bin/bash\nprintf "%s\\0" "$@"\n', encoding="utf-8")
    command.chmod(0o755)
    monkeypatch.setattr(scripts_main.importlib.metadata, "entry_points", lambda **_: [])
    scripts_main.create_script_symlinks(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MOONGAS_COLLECTION_ROOTDIR": str(tmp_path / "source"),
        "MOONGAS_COLLECTION_LOCAL": str(tmp_path / "local"),
        "COPY_MEDIALIB_EXCLUDE_KEYWORDS": "",
    }
    result = subprocess.run(
        [str(tmp_path / "copy-media-files"), "LoCaL", "--dry-run"],
        env=env, capture_output=True, check=True,
    )
    args = result.stdout.decode().split("\0")[:-1]
    assert args[args.index("--dst-path") + 1] == f"{tmp_path / 'local' / 'data'}/"
    assert args[-1] == "--dry-run"


def test_copy_failure_is_propagated(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "copy-medialib"
    command.write_text("#!/bin/bash\nexit 42\n", encoding="utf-8")
    command.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MOONGAS_COLLECTION_ROOTDIR": str(tmp_path / "source"),
        "MOONGAS_COLLECTION_DEMO": str(tmp_path / "demo"),
        "COPY_MEDIALIB_EXCLUDE_KEYWORDS": "",
    }
    result = subprocess.run([str(SHELL_DIR / "copy_media_files.sh"), "demo"], env=env)
    assert result.returncode == 42


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
@pytest.mark.parametrize("collection", ["demo", "local", "DEMO", "LoCaL", "Archive_2"])
@pytest.mark.parametrize(
    "keywords, expected",
    [("", []), ("\nFirst Artist\n\nSecond Artist\n", ["First Artist", "Second Artist"])],
)
def test_copy_scripts_forward_exclusions(
    tmp_path: Path, script: Path, keywords: str, expected: list[str], collection: str,
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
        "MOONGAS_COLLECTION_ARCHIVE_2": str(tmp_path / "archive_2"),
        "MOONGAS_COLLECTION_ROOTDIR": str(tmp_path / "active source"),
        "COPY_MEDIALIB_EXCLUDE_KEYWORDS": keywords,
    }

    result = subprocess.run(
        ["bash", str(script), collection, "--dry-run"], env=env, capture_output=True, check=True,
    )
    args = result.stdout.decode().split("\0")[:-1]
    index = args.index("--exclude-keywords")
    assert args[index + 1:] == [*expected, "--diff", "--dry-run"]
    destination = collection.lower()
    assert args[args.index("--src-paths") + 1] == str(tmp_path / "active source" / "data" / "Music")
    assert args[args.index("--dst-path") + 1] == f"{tmp_path / destination / 'data'}/"
    includes = {
        "album_yaml": ["album.yml"],
        "all_yaml": ["*.yml"],
        "artist_yaml": ["artist.yml"],
        "lyrics": ["*.lrc", "*.txt"],
        "media": ["*.mp3", "*.m4a"],
    }
    kind = script.name.removeprefix("copy_").removesuffix("_files.sh")
    include_index = args.index("--include-filenames")
    assert args[include_index + 1:args.index("--dir-copy-mode")] == includes[kind]
    assert ("--overwrite-existing" in args) == (kind != "media")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_copy_scripts_require_exclusion_variable(tmp_path: Path, script: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key != "COPY_MEDIALIB_EXCLUDE_KEYWORDS"}
    env["MOONGAS_COLLECTION_LOCAL"] = str(tmp_path / "local")
    env["MOONGAS_COLLECTION_DEMO"] = str(tmp_path / "demo")
    env["MOONGAS_COLLECTION_ROOTDIR"] = str(tmp_path / "source")
    result = subprocess.run(["bash", str(script), "demo"], env=env, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "COPY_MEDIALIB_EXCLUDE_KEYWORDS: unbound variable" in result.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
@pytest.mark.parametrize("root", [None, ""])
def test_copy_scripts_require_active_source(
    tmp_path: Path, script: Path, root: str | None,
) -> None:
    env = {
        **os.environ,
        "MOONGAS_COLLECTION_LOCAL": str(tmp_path / "local"),
        "MOONGAS_COLLECTION_DEMO": str(tmp_path / "demo"),
        "COPY_MEDIALIB_EXCLUDE_KEYWORDS": "",
    }
    if root is None:
        env.pop("MOONGAS_COLLECTION_ROOTDIR", None)
    else:
        env["MOONGAS_COLLECTION_ROOTDIR"] = root
    result = subprocess.run([str(script), "demo", "--delete"], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "MOONGAS_COLLECTION_ROOTDIR must be set" in result.stderr
    assert not (tmp_path / "local").exists()
    assert not (tmp_path / "demo").exists()


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
@pytest.mark.parametrize("value", [None, ""])
@pytest.mark.parametrize("collection", ["DEMO", "LoCaL", "Archive_2"])
def test_copy_scripts_require_destination(
    tmp_path: Path, script: Path, value: str | None, collection: str,
) -> None:
    destination_var = f"MOONGAS_COLLECTION_{collection.upper()}"
    env = {
        **os.environ,
        "MOONGAS_COLLECTION_ROOTDIR": str(tmp_path / "source"),
        "COPY_MEDIALIB_EXCLUDE_KEYWORDS": "",
    }
    if value is None:
        env.pop(destination_var, None)
    else:
        env[destination_var] = value
    result = subprocess.run([str(script), collection], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert f"{destination_var} must be set" in result.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
@pytest.mark.parametrize("collection", [None, "", "--dry-run", "invalid-name", "test name", "../local"])
def test_copy_scripts_reject_missing_or_invalid_collection(
    tmp_path: Path, script: Path, collection: str | None,
) -> None:
    env = {
        **os.environ,
        "MOONGAS_COLLECTION_ROOTDIR": str(tmp_path / "source"),
        "COPY_MEDIALIB_EXCLUDE_KEYWORDS": "",
    }
    args = [str(script)] if collection is None else [str(script), collection]
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert ("Usage:" if collection is None else "Invalid collection name") in result.stderr


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
        ("copy_lyrics_files.sh", "song.txt"),
        ("copy_lyrics_files.sh", "song.lrc"),
        ("copy_media_files.sh", "song.mp3"),
        ("copy_media_files.sh", "song.m4a"),
        ("copy_artist_yaml_files.sh", "artist.yml"),
        ("copy_album_yaml_files.sh", "album.yml"),
        ("copy_all_yaml_files.sh", "track.yml"),
    ],
)
@pytest.mark.parametrize("destination", ["local", "demo"])
def test_copy_scripts_exclude_matching_files(
    tmp_path: Path, copy_command_path: str, script: str, filename: str, destination: str,
) -> None:
    source = "active"
    for artist in ("Excluded Artist", "Other Blocked", "Included Artist"):
        track = tmp_path / source / "data" / "Music" / artist / "Album" / filename
        track.parent.mkdir(parents=True)
        track.write_text("test data", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": copy_command_path,
        "MOONGAS_COLLECTION_LOCAL": str(tmp_path / "local"),
        "MOONGAS_COLLECTION_DEMO": str(tmp_path / "demo"),
        "MOONGAS_COLLECTION_ROOTDIR": str(tmp_path / source),
        "COPY_MEDIALIB_EXCLUDE_KEYWORDS": "Excluded Artist\nOther Blocked",
    }

    subprocess.run(["bash", str(SHELL_DIR / script), destination], env=env, capture_output=True, check=True)
    destination_music = tmp_path / destination / "data" / "Music"
    assert (destination_music / "Included Artist" / "Album" / filename).read_text() == "test data"
    assert not (destination_music / "Excluded Artist" / "Album" / filename).exists()
    assert not (destination_music / "Other Blocked" / "Album" / filename).exists()


@pytest.mark.parametrize("keywords", ["", "\nExcluded Artist\n\n"])
@pytest.mark.parametrize("symlink_source", [False, True])
@pytest.mark.parametrize("target", ["demo", "local"])
def test_copy_all_yaml_wrapper_deletes_only_unexcluded_orphans(
    tmp_path: Path, copy_command_path: str, keywords: str, symlink_source: bool, target: str,
) -> None:
    source = tmp_path / "active" / "data" / "Music"
    destination = tmp_path / target / "data" / "Music"
    album = Path("The Strokes") / "Room On Fire [2003]"
    if symlink_source:
        library = tmp_path / "library"
        library.mkdir()
        source.parent.mkdir(parents=True)
        source.symlink_to(library, target_is_directory=True)
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
        "MOONGAS_COLLECTION_ROOTDIR": str(tmp_path / "active"),
        "COPY_MEDIALIB_EXCLUDE_KEYWORDS": keywords,
    }
    command = [str(SHELL_DIR / "copy_all_yaml_files.sh"), target, "--delete", "-q"]

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
