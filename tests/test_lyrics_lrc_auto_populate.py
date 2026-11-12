import logging
import sys
from dataclasses import dataclass
from collections.abc import Iterable
from pathlib import Path

import pytest

from mediascripts.common import files
from mediascripts.lyrics import lyrics_common
from mediascripts.lyrics.lyrics_lrc import lyrics_lrc_auto_populate as lrc


@dataclass
class Segment:
    start: float
    text: str


class Model:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def transcribe(
        self, audio: str, *, language: str,
    ) -> tuple[Iterable[Segment], object]:
        self.calls.append((audio, language))
        return iter([Segment(1.235, " First line"), Segment(61.5, "Second line")]), object()


def make_track(directory: Path, name: str = "01 - Track.mp3", text: str = "First line\nSecond line\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    track = directory / name
    track.touch()
    track.with_suffix(".txt").write_text(text, encoding="utf-8")
    return track


@pytest.mark.parametrize("suffix", [".mp3", ".yml", ".yaml", ".YAML"])
def test_file_argument_aligns_media_for_media_or_yaml_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    track = make_track(tmp_path, "01 - Track.live.mp3")
    target = track.with_suffix(suffix)
    if target != track:
        target.write_text("YAML contents are not used")
    model = Model()

    class FasterWhisper:
        def WhisperModel(
            self, name: str, *, device: str, compute_type: str,
        ) -> Model:
            return model

    monkeypatch.setattr(lrc, "import_module", lambda name: FasterWhisper())
    monkeypatch.setattr(sys, "argv", ["lyrics-lrc-auto-populate", "--file", str(target)])
    assert lrc.main() == 0
    assert model.calls == [(str(track), "en")]
    assert target.with_suffix(".lrc").read_text() == "[00:01.24] First line\n[01:01.50] Second line\n"
    assert lrc.main() == 0
    assert len(model.calls) == 1


def test_yaml_without_media_reports_error_before_loading_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    target = tmp_path / "Track.yml"
    target.touch()
    target.with_suffix(".txt").write_text("Lyrics")

    def unexpected_import(name: str) -> None:
        pytest.fail("Missing media must be reported before loading faster-whisper")

    monkeypatch.setattr(lrc, "import_module", unexpected_import)
    assert lrc.main_loop(specific_file=target) == 1
    assert "No same-basename media file" in caplog.text
    assert not target.with_suffix(".lrc").exists()


@pytest.mark.parametrize(
    "available",
    [
        [".mp3", ".m4a", ".flac", ".mkv"],
        [".m4a", ".flac", ".mp4"],
        [".aac", ".mp4", ".webm"],
        [".mp4", ".webm", ".mkv"],
        [".webm", ".mkv"],
        [".mkv"],
    ],
)
def test_yaml_alignment_uses_preferred_available_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available: list[str],
) -> None:
    target = tmp_path / "Track.yml"
    target.touch()
    for suffix in reversed(available):
        make_track(tmp_path, f"Track{suffix}")
    model = Model()

    class FasterWhisper:
        def WhisperModel(
            self, name: str, *, device: str, compute_type: str,
        ) -> Model:
            return model

    monkeypatch.setattr(lrc, "import_module", lambda name: FasterWhisper())
    assert lrc.main_loop(specific_file=target) == 0
    assert [call[0] for call in model.calls] == [str(target.with_suffix(available[0]))]
    assert target.with_suffix(".lrc").exists()


def test_directory_selection_requires_media_and_missing_lrc(tmp_path: Path) -> None:
    eligible = make_track(tmp_path, "01 - Track.MP3")
    existing = make_track(tmp_path, "02 - Existing.flac")
    existing.with_suffix(".lrc").write_text("existing")
    (tmp_path / "03 - No text.mp3").touch()
    (tmp_path / "04 - Metadata.yml").touch()
    (tmp_path / "04 - Metadata.txt").touch()
    make_track(tmp_path / "nested")
    (tmp_path / "directory.mp3").mkdir()
    (tmp_path / "directory.txt").touch()
    assert lrc.find_tracks_missing_lrc(tmp_path) == [eligible, tmp_path / "03 - No text.mp3"]


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
    class Aligner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def align(self, audio_path: str, text: str) -> list[lrc.TimedWord]:
            self.calls.append((audio_path, text))
            return [lrc.TimedWord(1.235), lrc.TimedWord(61.5), lrc.TimedWord(62.0), lrc.TimedWord(63.0)]

    model = Aligner()
    assert lrc.process_track(track, model, language="fr", use_lyrics_txt=True) == lrc.STATUS_SAVED
    assert model.calls == [(str(track), "First line\nSecond line")]
    assert track.with_suffix(".lrc").read_text() == (
        "[00:01.24] First line\n[01:02.00] Second line\n"
    )
    assert track.with_suffix(".txt").read_text(encoding="utf-8") == "\ufeffFirst line\nSecond line\n"
    assert not list(tmp_path.glob(".*.tmp"))


def test_use_lyrics_txt_loads_forced_aligner_and_preserves_line_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(tmp_path, text="First line\nSecond line\n")
    align_calls: list[tuple[list[dict[str, object]], str, str]] = []

    class WhisperX:
        def load_align_model(self, *, language_code: str, device: str) -> tuple[str, str]:
            assert (language_code, device) == ("en", "cpu")
            return "align-model", "metadata"

        def load_audio(self, path: str) -> bytes:
            assert path == str(track)
            return bytes(32000)

        def align(
            self,
            transcript: list[dict[str, object]],
            model: object,
            metadata: object,
            audio: bytes,
            device: str,
            *,
            return_char_alignments: bool,
        ) -> dict[str, object]:
            align_calls.append((transcript, device, str(return_char_alignments)))
            assert model == "align-model"
            assert metadata == "metadata"
            assert len(audio) == 32000
            return {
                "word_segments": [
                    {"word": "First", "start": 0.1},
                    {"word": "line", "start": 0.2},
                    {"word": "Second", "start": 1.3},
                    {"word": "line", "start": 1.4},
                ],
            }

    monkeypatch.setattr(lrc, "import_module", lambda name: WhisperX())
    monkeypatch.setattr(sys, "argv", [
        "lyrics-lrc-auto-populate", "--file", str(track), "--device", "cpu",
        "--use-lyrics-txt",
    ])
    assert lrc.main() == 0
    assert align_calls == [
        ([{"start": 0.0, "end": 2.0, "text": "First line Second line"}], "cpu", "False"),
    ]
    assert track.with_suffix(".lrc").read_text() == (
        "[00:00.10] First line\n[00:01.30] Second line\n"
    )


def test_use_lyrics_txt_alignment_handles_languages_without_spaces() -> None:
    words = [lrc.TimedWord(index / 10) for index in range(7)]
    assert lrc.format_lyrics_text_lrc(
        "こんにちは\n世界", words, language="ja",
    ) == "[00:00.00] こんにちは\n[00:00.50] 世界\n"


def test_use_lyrics_txt_requires_sibling_text_for_explicit_file(tmp_path: Path) -> None:
    track = tmp_path / "Track.mp3"
    track.touch()
    assert lrc.main_loop(specific_file=track, use_lyrics_txt=True) == 1


@pytest.mark.parametrize("collection", [False, True])
def test_batch_processes_eligible_tracks_in_shuffled_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collection: bool,
) -> None:
    album = tmp_path / "data" / "Music" / "Artist" / "Album [2000]"
    first = make_track(album, "01 - First.mp3")
    second = make_track(album, "02 - Second.mp3")
    existing = make_track(album, "03 - Existing.mp3")
    existing.with_suffix(".lrc").write_text("existing")
    model = Model()
    shuffled: list[list[Path]] = []

    def reverse_order(paths: list[Path]) -> None:
        shuffled.append(paths.copy())
        paths.reverse()

    class FasterWhisper:
        def WhisperModel(
            self, name: str, *, device: str, compute_type: str,
        ) -> Model:
            return model

    monkeypatch.setattr(lrc, "MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    monkeypatch.setattr(lrc, "import_module", lambda name: FasterWhisper())
    monkeypatch.setattr(files.random, "shuffle", reverse_order)
    assert lrc.main_loop(specific_dir=None if collection else album) == 0
    assert shuffled == [[first, second]]
    assert [call[0] for call in model.calls] == [str(second), str(first)]
    assert existing.with_suffix(".lrc").read_text() == "existing"


def test_listing_keeps_sorted_order_without_shuffling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    second = make_track(tmp_path, "02 - Second.mp3")
    first = make_track(tmp_path, "01 - First.mp3")

    def unexpected_shuffle(paths: list[Path]) -> None:
        pytest.fail("Listing must not shuffle tracks")

    monkeypatch.setattr(files.random, "shuffle", unexpected_shuffle)
    with caplog.at_level(logging.INFO):
        assert lrc.main_loop(align_enabled=False, specific_dir=tmp_path) == 0
    missing = [record.getMessage() for record in caplog.records if record.getMessage().startswith("Missing:")]
    assert missing == [f"Missing: {first.with_suffix('.lrc')}", f"Missing: {second.with_suffix('.lrc')}"]


@pytest.mark.parametrize("reason", ["existing", "no-text", "no-media", "broken-link"])
def test_ineligible_tracks_never_transcribe(tmp_path: Path, reason: str) -> None:
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
    assert lrc.process_track(track, model, use_lyrics_txt=reason == "no-text") == lrc.STATUS_SKIPPED
    assert not model.calls
    if reason == "existing":
        assert destination.read_text() == "existing"


def test_concurrent_lrc_is_not_overwritten(tmp_path: Path) -> None:
    track = make_track(tmp_path)

    class ConcurrentModel(Model):
        def transcribe(
            self, audio: str, *, language: str,
        ) -> tuple[Iterable[Segment], object]:
            track.with_suffix(".lrc").write_text("created during alignment")
            return super().transcribe(audio, language=language)

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
        def align(self, audio_path: str, text: str) -> list[lrc.TimedWord]:
            if failure == "runtime":
                raise RuntimeError("Alignment failed")
            if failure == "none":
                return []
            return [lrc.TimedWord(1.0) for _ in text.split()]

    if failure == "write":
        def fail_link(source: Path, target: Path) -> None:
            raise OSError("Disk failure")
        monkeypatch.setattr(lyrics_common.os, "link", fail_link)
    assert lrc.process_track(track, FailingModel(), use_lyrics_txt=True) == lrc.STATUS_ERROR
    assert "Failed to generate LRC lyrics" in caplog.text
    assert not track.with_suffix(".lrc").exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_lrc_timestamp_rounding_carries_into_next_minute() -> None:
    assert lrc.format_lrc([Segment(59.999, "line")]) == "[01:00.00] line\n"


@pytest.mark.parametrize(
    "segments",
    [[], [Segment(0, " ")], [Segment(-1, "line")], [Segment(float("nan"), "line")],
     [Segment(float("inf"), "line")], [Segment(2, "one"), Segment(1, "two")],
     [Segment(0, "two\nlines")]],
)
def test_invalid_transcription_timestamps_are_not_exported(segments: list[Segment]) -> None:
    with pytest.raises(ValueError):
        lrc.format_lrc(segments)


@pytest.mark.parametrize("target", ["file", "dir", "none"])
def test_listing_or_no_work_never_imports_faster_whisper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, target: str,
) -> None:
    track = make_track(tmp_path)

    def unexpected_import(name: str) -> None:
        pytest.fail("Whisper must not be imported in listing mode or with no eligible tracks")

    monkeypatch.setattr(lrc, "import_module", unexpected_import)
    kwargs = {"specific_file": track} if target == "file" else {"specific_dir": tmp_path}
    if target == "none":
        track.with_suffix(".lrc").touch()
    with caplog.at_level(logging.INFO):
        assert lrc.main_loop(align_enabled=target == "none", **kwargs) == 0
    if target != "none":
        assert str(track.with_suffix(".lrc")) in caplog.text


def test_batch_loads_model_once_continues_after_error_and_returns_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    make_track(tmp_path, "01 - Empty.mp3", text="")
    valid = make_track(tmp_path, "02 - Valid.mp3")
    loads: list[tuple[str, str, str]] = []

    class MixedModel(Model):
        def transcribe(
            self, audio: str, *, language: str,
        ) -> tuple[Iterable[Segment], object]:
            if audio.endswith("Empty.mp3"):
                return iter([]), object()
            return super().transcribe(audio, language=language)

    model = MixedModel()

    class FasterWhisper:
        def WhisperModel(
            self, name: str, *, device: str, compute_type: str,
        ) -> Model:
            loads.append((name, device, compute_type))
            return model

    monkeypatch.setattr(lrc, "import_module", lambda name: FasterWhisper())
    with caplog.at_level(logging.INFO):
        assert lrc.main_loop(specific_dir=tmp_path, model_name="medium", device="cpu") == 1
    assert loads == [("medium", "cpu", "int8")]
    assert len(model.calls) == 1
    assert valid.with_suffix(".lrc").exists()
    assert "Batch complete: 1 saved, 0 skipped, 1 errors" in caplog.text


@pytest.mark.parametrize("failure", ["import", "load"])
def test_model_setup_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str,
) -> None:
    track = make_track(tmp_path)

    class FasterWhisper:
        def WhisperModel(
            self, name: str, *, device: str, compute_type: str,
        ) -> Model:
            raise RuntimeError("Model could not load")

    def load(name: str) -> FasterWhisper:
        if failure == "import":
            raise ImportError("faster_whisper is not installed")
        return FasterWhisper()

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
        path = tmp_path / ("missing.mp3" if target == "missing-file" else "metadata.json")
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


@pytest.mark.parametrize(
    "options,expected_device",
    [
        ([], None),
        (["--device", "cuda"], "cuda"),
        (["--cpu"], "cpu"),
        (["--cpu", "--device", "cuda"], "cpu"),
        (["--device", "cuda", "--cpu"], "cpu"),
    ],
)
def test_cli_cpu_flag_controls_model_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    options: list[str], expected_device: str | None,
) -> None:
    track = make_track(tmp_path)
    devices: list[tuple[str, str, str]] = []

    class FasterWhisper:
        def WhisperModel(
            self, name: str, *, device: str, compute_type: str,
        ) -> Model:
            devices.append((name, device, compute_type))
            return Model()

    monkeypatch.setattr(lrc, "import_module", lambda name: FasterWhisper())
    monkeypatch.setattr(sys, "argv", ["lyrics-lrc-auto-populate", "--file", str(track), *options])
    assert lrc.main() == 0
    assert devices == [("small", expected_device or "auto", "int8")]
    assert track.with_suffix(".lrc").exists()


def test_cli_rejects_conflicting_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["lyrics-lrc-auto-populate", "--file", "song.mp3", "--dir", "."])
    with pytest.raises(SystemExit) as exc:
        lrc.main()
    assert exc.value.code == 2
