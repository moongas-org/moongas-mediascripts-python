import logging
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from mediascripts.lyrics.lyrics_text import lyrics_text_auto_populate as combined
from mediascripts.lyrics.lyrics_text import lyrics_text_auto_populate_from_file as transcription
from mediascripts.lyrics.lyrics_text import lyrics_text_common as common


class Segment:
    start = 0.0
    text = "Lyrics"


class Model:
    def transcribe(self, audio: str, *, language: str) -> tuple[Iterable[Segment], object]:
        return [Segment()], object()


class Whisper:
    utils = SimpleNamespace(_MODELS={"small": "Systran/faster-whisper-small", "large": "vendor/large-v3"})

    def __init__(self) -> None:
        self.loads: list[tuple[str, str, str]] = []

    def WhisperModel(self, name: str, *, device: str, compute_type: str) -> Model:
        self.loads.append((name, device, compute_type))
        return Model()


def install_whisper(monkeypatch: pytest.MonkeyPatch, whisper: Whisper) -> None:
    def load(name: str) -> Whisper:
        assert name == "faster_whisper"
        return whisper

    monkeypatch.setattr(common, "import_module", load)


def no_network(*args: object, **kwargs: object) -> None:
    pytest.fail("No network request expected")


@pytest.mark.parametrize("command", ["combined", "file"])
def test_text_commands_log_model_metadata_and_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, command: str,
) -> None:
    track = tmp_path / "Track.mp3"
    track.touch()
    whisper = Whisper()
    install_whisper(monkeypatch, whisper)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    calls: list[tuple[str, int]] = []

    def head(url: str, *, timeout: int) -> requests.Response:
        calls.append((url, timeout))
        response = requests.Response()
        response.status_code = 302
        response.headers["X-Linked-Size"] = str(2 * 1024 * 1024)
        return response

    monkeypatch.setattr(common.requests, "head", head)
    with caplog.at_level(logging.INFO):
        if command == "combined":
            result = combined.main_loop(lookup_enabled=False, specific_file=track, model_name="large", device="cpu")
        else:
            result = transcription.main_loop(specific_file=track, model_name="large", device="cpu")
    assert result == 0
    assert calls == [("https://huggingface.co/vendor/large-v3/resolve/main/model.bin", 5)]
    assert whisper.loads == [("large", "cpu", "int8")]
    assert "Checking model repository: vendor/large-v3" in caplog.text
    assert "Model file vendor/large-v3/model.bin size: 2.00 MiB" in caplog.text
    assert "Loading faster-whisper model large (device=cpu, compute_type=int8)" in caplog.text
    assert "faster-whisper model large loaded successfully" in caplog.text
    assert track.with_suffix(".txt").read_text() == "Lyrics\n"


@pytest.mark.parametrize("failure", ["network", "http", "invalid", "missing"])
def test_metadata_failure_does_not_prevent_model_loading(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str,
) -> None:
    whisper = Whisper()
    install_whisper(monkeypatch, whisper)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)

    def head(url: str, *, timeout: int) -> requests.Response:
        if failure == "network":
            raise requests.Timeout("metadata timeout")
        response = requests.Response()
        response.status_code = 403 if failure == "http" else 302
        if failure == "invalid":
            response.headers["X-Linked-Size"] = "not-a-number"
        return response

    monkeypatch.setattr(common.requests, "head", head)
    with caplog.at_level(logging.INFO):
        assert isinstance(common.load_transcription_model("small", None), Model)
    assert whisper.loads == [("small", "auto", "int8")]
    assert "file size unavailable" in caplog.text or "Could not check remote model file size" in caplog.text
    assert "loaded successfully" in caplog.text


def test_local_model_size_is_logged_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "model.bin").write_bytes(b"test")
    whisper = Whisper()
    install_whisper(monkeypatch, whisper)
    monkeypatch.setattr(common.requests, "head", no_network)
    with caplog.at_level(logging.INFO):
        common.load_transcription_model(str(tmp_path), None)
    assert f"Local model file {tmp_path / 'model.bin'} size: 0.00 MiB" in caplog.text
    assert whisper.loads == [(str(tmp_path), "auto", "int8")]


def test_offline_mode_skips_metadata_but_loads_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    whisper = Whisper()
    install_whisper(monkeypatch, whisper)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(common.requests, "head", no_network)
    with caplog.at_level(logging.INFO):
        common.load_transcription_model("small", None)
    assert "offline mode enabled" in caplog.text
    assert whisper.loads == [("small", "auto", "int8")]


def test_custom_repository_and_direct_content_length(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    whisper = Whisper()
    install_whisper(monkeypatch, whisper)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setenv("HF_ENDPOINT", "https://hub.example")

    def head(url: str, *, timeout: int) -> requests.Response:
        assert url == "https://hub.example/artist/model/resolve/main/model.bin"
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Length"] = str(1024 * 1024)
        return response

    monkeypatch.setattr(common.requests, "head", head)
    with caplog.at_level(logging.INFO):
        common.load_transcription_model("artist/model", None)
    assert "Model file artist/model/model.bin size: 1.00 MiB" in caplog.text


def test_failed_model_load_is_logged_without_success(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenWhisper(Whisper):
        def WhisperModel(self, name: str, *, device: str, compute_type: str) -> Model:
            raise RuntimeError("model load failed")

    install_whisper(monkeypatch, BrokenWhisper())
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="model load failed"):
        common.load_transcription_model("small", None)
    assert "Could not load faster-whisper model small" in caplog.text
    assert "loaded successfully" not in caplog.text
