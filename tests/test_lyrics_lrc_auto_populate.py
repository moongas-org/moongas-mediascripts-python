import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from mediascripts.lyrics.lyrics_lrc import lyrics_lrc_auto_populate as lrc


@dataclass
class Segment:
    start: float
    text: str


@dataclass
class Result:
    segments: list[Segment]


class Model:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, bool]] = []

    def align(
        self, audio: str, text: str, *, language: str, original_split: bool,
    ) -> Result:
        self.calls.append((audio, text, language, original_split))
        return Result([Segment(1.235, " First line"), Segment(61.5, "Second line")])


def make_track(directory: Path, name: str = "01 - Track.mp3", text: str = "First line\nSecond line\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    track = directory / name
    track.touch()
    track.with_suffix(".txt").write_text(text, encoding="utf-8")
    return track


def test_directory_selection_requires_media_text_and_missing_lrc(tmp_path: Path) -> None:
    eligible = make_track(tmp_path, "01 - Track.MP3")
    existing = make_track(tmp_path, "02 - Existing.flac")
    existing.with_suffix(".lrc").write_text("existing")
    (tmp_path / "03 - No text.mp3").touch()
    (tmp_path / "04 - Metadata.yml").touch()
    (tmp_path / "04 - Metadata.txt").touch()
    make_track(tmp_path / "nested")
    (tmp_path / "directory.mp3").mkdir()
    (tmp_path / "directory.txt").touch()
    assert lrc.find_tracks_missing_lrc(tmp_path) == [eligible]


def test_default_collection_scans_album_directories_and_library_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "collection"
    data = root / "data"
    data.mkdir(parents=True)
    library = tmp_path / "library"
    library.mkdir()
    track = make_track(library / "Artist" / "Album [2000]")
    make_track(data, "Not in an album.mp3")
    (data / "Music").symlink_to(library, target_is_directory=True)
    monkeypatch.setattr(lrc, "MOONGAS_COLLECTION_ROOTDIR", str(root))
    expected = data / "Music" / track.relative_to(library)
    assert lrc.find_tracks_missing_lrc(data, collection=True) == [expected]
    assert lrc.main_loop(align_enabled=False) == 0


def test_process_aligns_exact_existing_text_and_exports_lrc(tmp_path: Path) -> None:
    track = make_track(tmp_path, text="\ufeffFirst line\nSecond line\n")
    model = Model()
    assert lrc.process_track(track, model, language="fr") == lrc.STATUS_SAVED
    assert model.calls == [(str(track), "First line\nSecond line", "fr", True)]
    assert track.with_suffix(".lrc").read_text() == (
        "[00:01.24]First line\n[01:01.50]Second line\n"
    )
    assert track.with_suffix(".txt").read_text(encoding="utf-8") == "\ufeffFirst line\nSecond line\n"
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("reason", ["existing", "no-text", "no-media", "broken-link"])
def test_ineligible_tracks_never_align(tmp_path: Path, reason: str) -> None:
    track = make_track(tmp_path)
    destination = track.with_suffix(".lrc")
    if reason == "existing":
        destination.write_text("existing")
    elif reason == "no-text":
        track.with_suffix(".txt").unlink()
    elif reason == "no-media":
        track.unlink()
    else:
        destination.symlink_to(tmp_path / "missing.lrc")
    model = Model()
    assert lrc.process_track(track, model) == lrc.STATUS_SKIPPED
    assert not model.calls
    if reason == "existing":
        assert destination.read_text() == "existing"


def test_concurrent_lrc_is_not_overwritten(tmp_path: Path) -> None:
    track = make_track(tmp_path)

    class ConcurrentModel(Model):
        def align(self, audio: str, text: str, *, language: str, original_split: bool) -> Result:
            track.with_suffix(".lrc").write_text("created during alignment")
            return super().align(audio, text, language=language, original_split=original_split)

    assert lrc.process_track(track, ConcurrentModel()) == lrc.STATUS_SKIPPED
    assert track.with_suffix(".lrc").read_text() == "created during alignment"
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("failure", ["empty", "encoding", "none", "runtime", "write"])
def test_failed_tracks_log_errors_and_leave_no_lrc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, failure: str,
) -> None:
    track = make_track(tmp_path, text=" \n " if failure == "empty" else "Lyrics")
    if failure == "encoding":
        track.with_suffix(".txt").write_bytes(b"\xff")

    class FailingModel:
        def align(
            self, audio: str, text: str, *, language: str, original_split: bool,
        ) -> Result | None:
            if failure == "runtime":
                raise RuntimeError("Alignment failed")
            if failure == "none":
                return None
            return Result([Segment(1, text)])

    if failure == "write":
        def fail_link(source: Path, target: Path) -> None:
            raise OSError("Disk failure")
        monkeypatch.setattr(lrc.os, "link", fail_link)
    assert lrc.process_track(track, FailingModel()) == lrc.STATUS_ERROR
    assert "Failed to generate LRC lyrics" in caplog.text
    assert not track.with_suffix(".lrc").exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_lrc_timestamp_rounding_carries_into_next_minute() -> None:
    assert lrc.format_lrc(Result([Segment(59.999, "line")])) == "[01:00.00]line\n"


@pytest.mark.parametrize(
    "segments",
    [[], [Segment(0, " ")], [Segment(-1, "line")], [Segment(float("nan"), "line")],
     [Segment(float("inf"), "line")], [Segment(2, "one"), Segment(1, "two")],
     [Segment(0, "two\nlines")]],
)
def test_invalid_alignment_is_not_exported(segments: list[Segment]) -> None:
    with pytest.raises(ValueError):
        lrc.format_lrc(Result(segments))


@pytest.mark.parametrize("target", ["file", "dir", "none"])
def test_listing_or_no_work_never_imports_whisper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, target: str,
) -> None:
    track = make_track(tmp_path)

    def unexpected_import(name: str) -> None:
        pytest.fail("Whisper must not be imported in listing mode or with no eligible tracks")

    monkeypatch.setattr(lrc, "import_module", unexpected_import)
    kwargs = {"specific_file": track} if target == "file" else {"specific_dir": tmp_path}
    if target == "none":
        track.with_suffix(".txt").unlink()
    with caplog.at_level(logging.INFO):
        assert lrc.main_loop(align_enabled=target == "none", **kwargs) == 0
    if target != "none":
        assert str(track.with_suffix(".lrc")) in caplog.text


def test_batch_loads_model_once_continues_after_error_and_returns_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    make_track(tmp_path, "01 - Empty.mp3", text="")
    valid = make_track(tmp_path, "02 - Valid.mp3")
    loads: list[tuple[str, str | None]] = []
    model = Model()

    class Whisper:
        def load_model(self, name: str, *, device: str | None) -> Model:
            loads.append((name, device))
            return model

    monkeypatch.setattr(lrc, "import_module", lambda name: Whisper())
    with caplog.at_level(logging.INFO):
        assert lrc.main_loop(specific_dir=tmp_path, model_name="medium", device="cpu") == 1
    assert loads == [("medium", "cpu")]
    assert len(model.calls) == 1
    assert valid.with_suffix(".lrc").exists()
    assert "Batch complete: 1 saved, 0 skipped, 1 errors" in caplog.text


@pytest.mark.parametrize("failure", ["import", "load"])
def test_model_setup_failure_is_reported(
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

    monkeypatch.setattr(lrc, "import_module", load)
    assert lrc.main_loop(specific_file=track) == 1
    assert "Could not import" in caplog.text if failure == "import" else "Could not load" in caplog.text
    assert not track.with_suffix(".lrc").exists()


@pytest.mark.parametrize("target", ["missing-file", "unsupported-file", "missing-dir"])
def test_invalid_targets_return_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, target: str,
) -> None:
    if target == "missing-dir":
        result = lrc.main_loop(specific_dir=tmp_path / "missing")
    else:
        path = tmp_path / ("missing.mp3" if target == "missing-file" else "metadata.yml")
        if target == "unsupported-file":
            path.touch()
        result = lrc.main_loop(specific_file=path)
    assert result == 1
    assert caplog.records


@pytest.mark.parametrize("option", ["--align=false", "--lookup=false"])
def test_cli_listing_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str) -> None:
    track = make_track(tmp_path)
    monkeypatch.setattr(sys, "argv", ["lyrics-lrc-auto-populate", "--file", str(track), option])
    assert lrc.main() == 0
    assert not track.with_suffix(".lrc").exists()


def test_cli_rejects_conflicting_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["lyrics-lrc-auto-populate", "--file", "song.mp3", "--dir", "."])
    with pytest.raises(SystemExit) as exc:
        lrc.main()
    assert exc.value.code == 2
