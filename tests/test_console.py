import ast
import os
import select
import signal
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from mediascripts import console
from mediascripts import main as scripts_main


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]


def test_track_yaml_auto_create_replaces_old_entry_point() -> None:
    assert SCRIPTS["track-yaml-auto-create"] == "mediascripts.console:track_yaml_auto_create"
    assert "track-yaml-auto-populate-from-file" not in SCRIPTS
    assert console.track_yaml_auto_create.module == "mediascripts.track.track_yaml.track_yaml_auto_create"


def test_create_script_symlinks_uses_track_yaml_auto_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "track-yaml-auto-create"
    executable.parent.mkdir()
    executable.touch()
    destination = tmp_path / "collection"
    destination.mkdir()
    entry = SimpleNamespace(
        name="track-yaml-auto-create",
        value=SCRIPTS["track-yaml-auto-create"],
        dist=SimpleNamespace(name="mediascripts"),
    )
    monkeypatch.setattr(scripts_main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(scripts_main.importlib.metadata, "entry_points", lambda **_: [entry])
    monkeypatch.setattr(scripts_main.shutil, "which", lambda name: str(executable) if name == entry.name else None)

    scripts_main.create_script_symlinks(destination)

    assert (destination / "track-yaml-auto-create").resolve() == executable
    assert not (destination / "track-yaml-auto-populate-from-file").exists()


@pytest.mark.parametrize(("name", "entry_point"), SCRIPTS.items())
def test_every_console_script_routes_to_its_original_main(
    name: str, entry_point: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, attribute = entry_point.split(":")
    assert module == "mediascripts.console"
    assert attribute == name.replace("-", "_")
    script = getattr(console, attribute)
    assert isinstance(script, console.ConsoleScript)
    assert script.module.rsplit(".", 1)[-1] == ("__main__" if name == "mediascripts" else attribute)
    source = ROOT / "src" / (script.module.replace(".", "/") + ".py")
    assert source.is_file()
    tree = ast.parse(source.read_text(encoding="utf-8"))
    assert any(
        isinstance(node, (ast.FunctionDef, ast.ImportFrom))
        and (getattr(node, "name", None) == "main" or any(
            alias.name == "main" for alias in getattr(node, "names", [])
        ))
        for node in tree.body
    )
    imported: list[str] = []

    def fake_import(module_name: str) -> SimpleNamespace:
        imported.append(module_name)
        return SimpleNamespace(main=lambda: 7)

    monkeypatch.setattr(console.importlib, "import_module", fake_import)
    assert script() == 7
    assert imported == [script.module]


@pytest.mark.parametrize("stage", ["import", "main"])
def test_interrupt_returns_130_and_reports_it(
    stage: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupted() -> None:
        raise KeyboardInterrupt

    def fake_import(_module_name: str) -> SimpleNamespace:
        if stage == "import":
            interrupted()
        return SimpleNamespace(main=interrupted)

    monkeypatch.setattr(console.importlib, "import_module", fake_import)
    assert console.rename_album_files() == 130
    assert capsys.readouterr().err == "Interrupted by user; exiting\n"


def test_non_interrupt_errors_are_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_module_name: str) -> None:
        raise ValueError("broken")

    monkeypatch.setattr(console.importlib, "import_module", fail)
    with pytest.raises(ValueError, match="broken"):
        console.rename_album_files()


def test_list_preserves_script_groups(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    entries = [
        SimpleNamespace(name="rename-album-files", value=SCRIPTS["rename-album-files"],
                        dist=SimpleNamespace(name="mediascripts")),
        SimpleNamespace(name="other", value="mediascan.other.command:main",
                        dist=SimpleNamespace(name="mediascan")),
    ]
    def fake_entry_points(**_kwargs: object) -> list[SimpleNamespace]:
        return entries

    monkeypatch.setattr(scripts_main.importlib.metadata, "entry_points", fake_entry_points)
    scripts_main.list_media_scripts()
    assert capsys.readouterr().out == "other:\n  other\nrename:\n  rename-album-files\n"


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT delivery differs on Windows")
@pytest.mark.parametrize("stage", ["import", "main"])
def test_real_sigint_exits_without_traceback_and_runs_cleanup(
    tmp_path: Path, stage: str,
) -> None:
    cleanup = tmp_path / "cleaned"
    (tmp_path / "interrupt_target.py").write_text(
        "import time\n"
        "from pathlib import Path\n"
        f"cleanup = Path({str(cleanup)!r})\n"
        "def wait():\n"
        "    try:\n"
        "        print('READY', flush=True)\n"
        "        time.sleep(60)\n"
        "    finally:\n"
        "        cleanup.write_text('done')\n"
        + ("wait()\n" if stage == "import" else "def main():\n    wait()\n"),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(ROOT / "src")])
    process = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; from mediascripts.console import ConsoleScript; "
         "sys.exit(ConsoleScript('interrupt_target')())"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 15)
        assert ready and process.stdout.readline() == "READY\n"
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == 130
        assert stderr == "Interrupted by user; exiting\n"
        assert cleanup.read_text() == "done"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
