import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

from mediascripts.lyrics.lyrics_text import lyrics_text_auto_populate as combined
from mediascripts.lyrics.lyrics_text import lyrics_text_auto_populate_from_file as transcription
from mediascripts.lyrics.lyrics_text import lyrics_text_auto_populate_from_lrclib as lrclib


class Segment:
    def __init__(self, text: str) -> None:
        self.text = text
        self.start = 0.0


class Model:
    def __init__(self) -> None:
        self.transcribed: list[tuple[str, str]] = []

    def transcribe(
        self, audio: str, *, language: str,
    ) -> tuple[Iterable[Segment], object]:
        self.transcribed.append((audio, language))
        return iter([Segment("Fallback lyrics")]), object()


def make_track(directory: Path, name: str = "01 - Track.mp3") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    track = directory / name
    track.touch()
    return track


def setup_lrclib(
    monkeypatch: pytest.MonkeyPatch,
    process: object,
) -> None:
    monkeypatch.setattr(combined.lrclib, "load_lookup_cache", lambda: {})
    monkeypatch.setattr(combined.lrclib, "process_track", process)
    monkeypatch.setattr(combined.lrclib, "save_lookup_cache", lambda cache: None)
    monkeypatch.setattr(combined.lrclib, "wait_before_request", lambda: None)
    monkeypatch.setattr(combined.random, "shuffle", lambda tracks: None)


def test_saves_lrclib_result_without_loading_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(tmp_path)
    events: list[str] = []

    def lookup(path: Path) -> str:
        events.append("lrclib")
        path.with_suffix(".txt").write_text("LRCLIB lyrics\n", encoding="utf-8")
        return lrclib.STATUS_SAVED

    setup_lrclib(monkeypatch, lookup)
    monkeypatch.setattr(
        combined, "_load_transcription_model",
        lambda *args: pytest.fail("A successful LRCLIB result must not load a model"),
    )
    assert combined.main_loop(specific_file=track) == 0
    assert events == ["lrclib"]
    assert track.with_suffix(".txt").read_text(encoding="utf-8") == "LRCLIB lyrics\n"


@pytest.mark.parametrize(
    "lookup_status",
    [
        lrclib.STATUS_NOT_FOUND,
        lrclib.STATUS_INSTRUMENTAL,
        lrclib.STATUS_ERROR,
        lrclib.STATUS_RETRY,
    ],
)
def test_falls_back_to_media_after_lrclib_does_not_find_lyrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lookup_status: str,
) -> None:
    track = make_track(tmp_path)
    events: list[str] = []
    model = Model()

    def lookup(path: Path) -> str:
        events.append("lrclib")
        return lookup_status

    def load_model(model_name: str, device: str | None) -> Model:
        events.append("load-model")
        assert model_name == "medium"
        assert device == "cpu"
        return model

    def transcribe(path: Path, actual_model: Model, language: str) -> str:
        events.append("media")
        assert actual_model is model
        assert language == "fr"
        return transcription.STATUS_SAVED

    setup_lrclib(monkeypatch, lookup)
    monkeypatch.setattr(combined.lrclib, "LRCLIB_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(combined, "_load_transcription_model", load_model)
    monkeypatch.setattr(combined.media_transcription, "process_track", transcribe)

    assert combined.main_loop(
        specific_file=track, model_name="medium", language="fr", device="cpu",
    ) == 0
    assert events == ["lrclib", "load-model", "media"]
    assert model.transcribed == []


def test_cached_not_found_skips_lrclib_but_still_tries_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(tmp_path)
    key = str(track.resolve())
    cache = {key: {"status": lrclib.STATUS_NOT_FOUND}}
    monkeypatch.setattr(combined.lrclib, "load_lookup_cache", lambda: cache)
    monkeypatch.setattr(
        combined.lrclib, "process_track",
        lambda path: pytest.fail("Cached not-found result should skip LRCLIB"),
    )
    monkeypatch.setattr(combined.lrclib, "save_lookup_cache", lambda saved: None)
    monkeypatch.setattr(combined.random, "shuffle", lambda tracks: None)
    model = Model()
    monkeypatch.setattr(combined, "_load_transcription_model", lambda *args: model)
    monkeypatch.setattr(
        combined.media_transcription, "process_track",
        lambda path, actual_model, language: transcription.STATUS_SAVED,
    )
    assert combined.main_loop(specific_file=track) == 0


def test_fallback_transcription_uses_artist_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    artist = tmp_path / "Artist"
    track = make_track(artist / "Album [2000]")
    (artist / "artist.yml").write_text(
        "artistData:\n  artistNames: [Artist]\n  city: ''\n  countryCode: ''\n"
        "  regionCode: ''\n  languageCodes: [es, en]\n",
        encoding="utf-8",
    )
    setup_lrclib(monkeypatch, lambda path: lrclib.STATUS_NOT_FOUND)
    model = Model()
    monkeypatch.setattr(combined, "_load_transcription_model", lambda *args: model)
    assert combined.main_loop(specific_file=track, language="fr") == 0
    assert model.transcribed == [(str(track), "es")]


def test_transcription_failure_is_reported_after_lrclib_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(tmp_path)
    setup_lrclib(monkeypatch, lambda path: lrclib.STATUS_NOT_FOUND)
    monkeypatch.setattr(combined, "_load_transcription_model", lambda *args: Model())
    monkeypatch.setattr(
        combined.media_transcription, "process_track",
        lambda *args: transcription.STATUS_ERROR,
    )
    assert combined.main_loop(specific_file=track) == 1


def test_cli_accepts_the_union_of_lookup_and_transcription_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(tmp_path)
    captured: dict[str, object] = {}

    def main_loop(**options: object) -> int:
        captured.update(options)
        return 0

    monkeypatch.setattr(combined, "main_loop", main_loop)
    monkeypatch.setattr(sys, "argv", [
        "lyrics-text-auto-populate", "--file", str(track),
        "--sleep", "0.25", "--lookup=false", "--transcribe=true",
        "--retry-not-found", "--model", "medium", "--language", "fr",
        "--cpu",
    ])
    assert combined.main() == 0
    assert captured == {
        "sleep_between_requests": 0.25,
        "lookup_enabled": False,
        "transcribe_enabled": True,
        "specific_file": track,
        "specific_dir": None,
        "retry_not_found": True,
        "model_name": "medium",
        "language": "fr",
        "device": "cpu",
    }


def test_cli_entry_point_is_registered() -> None:
    import tomllib

    project_file = Path(__file__).parents[1] / "pyproject.toml"
    config = tomllib.loads(project_file.read_text(encoding="utf-8"))
    assert config["project"]["scripts"]["lyrics-text-auto-populate"] == (
        f"{combined.__name__}:main"
    )
