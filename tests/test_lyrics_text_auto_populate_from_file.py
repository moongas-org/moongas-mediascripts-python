import logging
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from subprocess import CalledProcessError

import pytest

from mediascripts.common import files
from mediascripts.lyrics import lyrics_common
from mediascripts.lyrics.lyrics_text import lyrics_text_auto_populate_from_file as text


@dataclass
class Segment:
    text: str


@dataclass
class Result:
    segments: list[Segment]


class Model:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def transcribe(self, audio: str, *, language: str) -> Result:
        self.calls.append((audio, language))
        return Result([Segment(" First line "), Segment(" "), Segment("Second line")])


def make_track(directory: Path, name: str = "01 - Track.mp3") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    track = directory / name
    track.touch()
    return track


def install_model(monkeypatch: pytest.MonkeyPatch, model: Model) -> list[tuple[str, str | None]]:
    loads: list[tuple[str, str | None]] = []

    class Whisper:
        def load_model(self, name: str, *, device: str | None) -> Model:
            loads.append((name, device))
            return model

    def import_whisper(name: str) -> Whisper:
        assert name == "stable_whisper"
        return Whisper()

    monkeypatch.setattr(text, "import_module", import_whisper)
    return loads


@pytest.mark.parametrize("suffix", [".mp3", ".yml", ".yaml", ".YAML"])
def test_file_argument_transcribes_media_or_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    track = make_track(tmp_path, "01 - Track.live.mp3")
    target = track.with_suffix(suffix)
    target.touch()
    model = Model()
    loads = install_model(monkeypatch, model)
    monkeypatch.setattr(sys, "argv", ["lyrics-text-auto-populate-from-file", "--file", str(target)])
    assert text.main() == 0
    assert model.calls == [(str(track), "en")]
    assert loads == [("small", None)]
    assert track.with_suffix(".txt").read_text(encoding="utf-8") == "First line\nSecond line\n"
    assert text.main() == 0
    assert len(model.calls) == len(loads) == 1
    assert not track.with_suffix(".lrc").exists()
    assert track.read_bytes() == b""


def test_directory_selection_ignores_lrc_and_requires_missing_text(tmp_path: Path) -> None:
    first = make_track(tmp_path, "01 - Track.MP3")
    second = make_track(tmp_path, "02 - With LRC.flac")
    second.with_suffix(".lrc").write_text("[00:00.00] Existing LRC")
    existing = make_track(tmp_path, "03 - Existing.mp3")
    existing.with_suffix(".txt").write_text("existing")
    (tmp_path / "04 - Metadata.yml").touch()
    (tmp_path / "directory.mp3").mkdir()
    make_track(tmp_path / "nested")
    assert text.find_tracks_missing_lyrics(tmp_path) == [first, second]


@pytest.mark.parametrize("collection", [False, True])
def test_batch_loads_once_and_shuffles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collection: bool,
) -> None:
    album = tmp_path / "data" / "Music" / "Artist" / "Album [2000]"
    first = make_track(album, "01 - First.mp3")
    second = make_track(album, "02 - Second.mp3")
    existing = make_track(album, "03 - Existing.mp3")
    existing.with_suffix(".txt").write_text("existing")
    model = Model()
    loads = install_model(monkeypatch, model)
    shuffled: list[list[Path]] = []

    def reverse(paths: list[Path]) -> None:
        shuffled.append(paths.copy())
        paths.reverse()

    monkeypatch.setattr(text, "MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    monkeypatch.setattr(files.random, "shuffle", reverse)
    assert text.main_loop(
        specific_dir=None if collection else album, model_name="medium", language="fr", device="cpu",
    ) == 0
    assert loads == [("medium", "cpu")]
    assert shuffled == [[first, second]]
    assert model.calls == [(str(second), "fr"), (str(first), "fr")]
    assert existing.with_suffix(".txt").read_text() == "existing"


def test_collection_follows_library_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "collection" / "data"
    data.mkdir(parents=True)
    library = tmp_path / "library"
    track = make_track(library / "Artist" / "Album [2000]")
    make_track(data, "Not an album.mp3")
    (data / "Music").symlink_to(library, target_is_directory=True)
    monkeypatch.setattr(text, "MOONGAS_COLLECTION_ROOTDIR", str(data.parent))
    assert text.find_tracks_missing_lyrics(data, collection=True) == [
        data / "Music" / track.relative_to(library),
    ]
    assert text.main_loop(transcribe_enabled=False) == 0


@pytest.mark.parametrize("option", ["--transcribe=false", "--lookup=false"])
def test_listing_does_not_import_or_shuffle_and_is_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, option: str,
) -> None:
    second = make_track(tmp_path, "02 - Second.mp3")
    first = make_track(tmp_path, "01 - First.mp3")

    def unexpected(*args: object) -> None:
        pytest.fail("Listing must not load Whisper or shuffle")

    monkeypatch.setattr(text, "import_module", unexpected)
    monkeypatch.setattr(files.random, "shuffle", unexpected)
    monkeypatch.setattr(sys, "argv", ["lyrics-text-auto-populate-from-file", "--dir", str(tmp_path), option])
    with caplog.at_level(logging.INFO):
        assert text.main() == 0
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("Missing:")] == [
        f"Missing: {first.with_suffix('.txt')}", f"Missing: {second.with_suffix('.txt')}",
    ]
    assert not first.with_suffix(".txt").exists()


@pytest.mark.parametrize("reason", ["existing", "broken-link", "no-media", "unsupported"])
def test_ineligible_tracks_never_transcribe(tmp_path: Path, reason: str) -> None:
    track = make_track(tmp_path, "Track.json" if reason == "unsupported" else "Track.mp3")
    destination = track.with_suffix(".txt")
    if reason == "existing":
        destination.write_text("existing")
    elif reason == "broken-link":
        destination.symlink_to(tmp_path / "missing.txt")
    elif reason == "no-media":
        track.unlink()
    model = Model()
    assert text.process_track(track, model) == text.STATUS_SKIPPED
    assert not model.calls
    if reason == "existing":
        assert destination.read_text() == "existing"


def test_concurrent_text_is_not_overwritten(tmp_path: Path) -> None:
    track = make_track(tmp_path)

    class RacingModel:
        def transcribe(self, audio: str, *, language: str) -> Result:
            track.with_suffix(".txt").write_text("created meanwhile")
            return Result([Segment("new text")])

    assert text.process_track(track, RacingModel()) == text.STATUS_SKIPPED
    assert track.with_suffix(".txt").read_text() == "created meanwhile"
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("failure", ["runtime", "ffmpeg", "none", "empty", "write"])
def test_processing_errors_do_not_publish_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str,
) -> None:
    track = make_track(tmp_path)

    class FailingModel:
        def transcribe(self, audio: str, *, language: str) -> Result | None:
            if failure == "runtime":
                raise RuntimeError("Transcription failed")
            if failure == "ffmpeg":
                raise CalledProcessError(1, ["ffmpeg"])
            if failure == "none":
                return None
            return Result([Segment(" " if failure == "empty" else "Lyrics")])

    if failure == "write":
        def fail_link(source: Path, destination: Path) -> None:
            raise OSError("Disk failure")
        monkeypatch.setattr(lyrics_common.os, "link", fail_link)
    assert text.process_track(track, FailingModel()) == text.STATUS_ERROR
    assert "Failed to transcribe lyrics" in caplog.text
    assert not track.with_suffix(".txt").exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_batch_continues_after_error_and_returns_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = make_track(tmp_path, "01 - Bad.mp3")
    good = make_track(tmp_path, "02 - Good.mp3")

    class MixedModel(Model):
        def transcribe(self, audio: str, *, language: str) -> Result:
            if audio == str(bad):
                raise RuntimeError("Bad audio")
            return super().transcribe(audio, language=language)

    install_model(monkeypatch, MixedModel())
    assert text.main_loop(specific_dir=tmp_path) == 1
    assert good.with_suffix(".txt").read_text() == "First line\nSecond line\n"
    assert not bad.with_suffix(".txt").exists()


@pytest.mark.parametrize("failure", ["import", "load"])
def test_setup_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str,
) -> None:
    track = make_track(tmp_path)

    class Whisper:
        def load_model(self, name: str, *, device: str | None) -> Model:
            raise RuntimeError("Model could not load")

    def load(name: str) -> Whisper:
        if failure == "import":
            raise ImportError("stable_whisper is not installed")
        return Whisper()

    monkeypatch.setattr(text, "import_module", load)
    assert text.main_loop(specific_file=track) == 1
    assert ("Could not import" if failure == "import" else "Could not load") in caplog.text
    assert not track.with_suffix(".txt").exists()


@pytest.mark.parametrize("target", ["missing-file", "unsupported-file", "missing-dir", "yaml-without-media"])
def test_invalid_targets_fail(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, target: str,
) -> None:
    if target == "missing-dir":
        result = text.main_loop(specific_dir=tmp_path / "missing")
    else:
        path = tmp_path / {
            "missing-file": "missing.mp3", "unsupported-file": "metadata.json", "yaml-without-media": "Track.yml",
        }[target]
        if target != "missing-file":
            path.touch()
        result = text.main_loop(specific_file=path)
    assert result == 1
    assert caplog.records


def test_no_work_does_not_load_whisper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(name: str) -> None:
        pytest.fail("No-work run must not load Whisper")

    monkeypatch.setattr(text, "import_module", unexpected)
    assert text.main_loop(specific_dir=tmp_path) == 0


@pytest.mark.parametrize(
    "options,device",
    [([], None), (["--device", "cuda"], "cuda"), (["--cpu"], "cpu"),
     (["--cpu", "--device", "cuda"], "cpu"), (["--device", "cuda", "--cpu"], "cpu")],
)
def test_cli_model_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, options: list[str], device: str | None,
) -> None:
    track = make_track(tmp_path)
    model = Model()
    loads = install_model(monkeypatch, model)
    monkeypatch.setattr(sys, "argv", [
        "lyrics-text-auto-populate-from-file", "--file", str(track),
        "--model", "medium", "--language", "fr", *options,
    ])
    assert text.main() == 0
    assert loads == [("medium", device)]
    assert model.calls == [(str(track), "fr")]


def test_cli_rejects_conflicting_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["lyrics-text-auto-populate-from-file", "--file", "track.mp3", "--dir", "."])
    with pytest.raises(SystemExit) as exc:
        text.main()
    assert exc.value.code == 2


def test_console_script_is_registered() -> None:
    config = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert config["project"]["scripts"]["lyrics-text-auto-populate-from-file"] == f"{text.__name__}:main"
