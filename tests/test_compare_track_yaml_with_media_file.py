import logging
import sys
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from mediascripts.track.track_yaml import compare_track_yaml_with_media_file as compare
from mediascripts.track.track_yaml.track_yaml_common import process_collection


@pytest.fixture(autouse=True)
def clear_excluded_artists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COPY_MEDIALIB_EXCLUDE_KEYWORDS", raising=False)


@pytest.fixture
def track(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "Music" / "Artist" / "Album [1995]" / "01 - Track.mp3"
    path.parent.mkdir(parents=True)
    tags = EasyID3()
    for key, value in {
        "title": "Track", "artist": "Artist", "albumartist": "Artist",
        "album": "Album", "genre": "Rock", "date": "1995-03-27",
    }.items():
        tags[key] = value
    tags.save(path)
    path.with_suffix(".yml").write_text(
        "trackData:\n"
        "  title: Track\n"
        "  artist: Artist\n"
        "  albumartist: Artist\n"
        "  album: Album\n"
        "  genre: Rock\n"
        "  year: 1995\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("quiet", [False, True])
def test_matching_real_id3_tags(
    track: Path, caplog: pytest.LogCaptureFixture, quiet: bool
) -> None:
    before = track.read_bytes()
    with caplog.at_level(logging.INFO):
        assert compare.compare_track(track, quiet=quiet)
    assert ("Track YAML matches" in caplog.text) is not quiet
    assert track.read_bytes() == before


@pytest.mark.parametrize("quiet", [False, True])
def test_excluded_artist_skipped_before_reading_media_tags(
    track: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, quiet: bool
) -> None:
    monkeypatch.setenv("COPY_MEDIALIB_EXCLUDE_KEYWORDS", "Unrelated\nArt\n\n")
    track.write_bytes(b"invalid media")
    with caplog.at_level(logging.INFO):
        assert compare.compare_track(track, quiet=quiet)
    assert caplog.text == ""


@pytest.mark.parametrize("keywords", ["", "\n \n", "Other\nSomeone", "artist"])
def test_nonmatching_keywords_do_not_skip_comparison(
    track: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, keywords: str
) -> None:
    monkeypatch.setenv("COPY_MEDIALIB_EXCLUDE_KEYWORDS", keywords)
    tags = EasyID3(track)
    tags["title"] = "Different"
    tags.save(track)
    assert not compare.compare_track(track, quiet=True)
    assert "title mismatch" in caplog.text


@pytest.mark.parametrize("yaml_text", [None, "[invalid"])
def test_excluded_artist_directory_skips_unreadable_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, yaml_text: str | None
) -> None:
    track = tmp_path / "Music" / "Sepulga" / "[2010] Untitled Recordings" / "rec00020.mp3"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"invalid media")
    if yaml_text is not None:
        track.with_suffix(".yml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("COPY_MEDIALIB_EXCLUDE_KEYWORDS", "Other\nSepulga")

    with caplog.at_level(logging.INFO):
        assert compare.compare_track(track)
        assert process_collection(tmp_path, compare.compare_track, album_directories_only=False)
    assert caplog.text == ""


def test_yaml_artist_exclusion_still_applies_outside_matching_directory(
    track: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    yaml_path = track.with_suffix(".yml")
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8").replace("artist: Artist", "artist: Sepulga"),
        encoding="utf-8",
    )
    monkeypatch.setenv("COPY_MEDIALIB_EXCLUDE_KEYWORDS", "Sepulga")
    track.write_bytes(b"invalid media")
    with caplog.at_level(logging.INFO):
        assert compare.compare_track(track)
    assert caplog.text == ""


@pytest.mark.parametrize("tag", ["title", "artist", "albumartist", "album", "genre", "date"])
def test_differences_are_reported_even_when_quiet(
    track: Path, caplog: pytest.LogCaptureFixture, tag: str
) -> None:
    tags = EasyID3(track)
    tags[tag] = "2000" if tag == "date" else "Different"
    tags.save(track)
    assert not compare.compare_track(track, quiet=True)
    assert "mismatch: YAML=" in caplog.text
    assert f"ID3 {tag}=" in caplog.text


def test_missing_and_extra_tag_values_are_not_hidden(
    track: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tags = EasyID3(track)
    del tags["albumartist"]
    tags["artist"] = ["Artist", "Other"]
    tags.save(track)
    assert not compare.compare_track(track, quiet=True)
    assert "ID3 albumartist=[]" in caplog.text
    assert "ID3 artist=['Artist', 'Other']" in caplog.text


@pytest.mark.parametrize("yaml_text", [None, "[invalid", "{}", "trackData: null", "[]", ""])
def test_missing_or_invalid_yaml_reports_error(
    track: Path, caplog: pytest.LogCaptureFixture, yaml_text: str | None
) -> None:
    yaml_path = track.with_suffix(".yml")
    if yaml_text is None:
        yaml_path.unlink()
    else:
        yaml_path.write_text(yaml_text, encoding="utf-8")
    assert not compare.compare_track(track, quiet=True)
    assert "Could not compare" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_invalid_media_reports_error(track: Path, caplog: pytest.LogCaptureFixture) -> None:
    track.write_bytes(b"invalid media")
    assert not compare.compare_track(track, quiet=True)
    assert "Could not compare" in caplog.text


def test_collection_follows_library_symlinks_and_ignores_non_tracks(
    track: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = tmp_path / "linked" / "data"
    data.mkdir(parents=True)
    (data / "Music").symlink_to(tmp_path / "data" / "Music", target_is_directory=True)
    (track.parent / "cover.jpg").touch()
    (track.parent / "album.yml").write_text("not track metadata", encoding="utf-8")
    (data / "not-an-album.mp3").touch()
    with caplog.at_level(logging.INFO):
        assert process_collection(data, compare.compare_track)
    assert caplog.text.count("Track YAML matches") == 1


@pytest.mark.parametrize("flag", ["-q", "--quiet"])
def test_quiet_cli_is_silent_when_matching(
    track: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["compare-track-yaml-with-media-file", flag])
    with caplog.at_level(logging.INFO):
        assert compare.main() == 0
    assert caplog.text == ""
    assert capsys.readouterr() == ("", "")


def test_cli_returns_failure_for_mismatch(
    track: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tags = EasyID3(track)
    tags["title"] = "Different"
    tags.save(track)
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["compare-track-yaml-with-media-file", "-q"])
    assert compare.main() == 1


@pytest.mark.parametrize("flag", ["-d", "--dir"])
@pytest.mark.parametrize("configured_env", [False, True])
def test_cli_directory_recurses_outside_collection_structure(
    track: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, flag: str, configured_env: bool
) -> None:
    directory = tmp_path / "arbitrary"
    nested = directory / "nested" / "deeper"
    nested.mkdir(parents=True)
    for destination in (directory / "Root.mp3", nested / "Nested.mp3"):
        destination.write_bytes(track.read_bytes())
        destination.with_suffix(".yml").write_bytes(track.with_suffix(".yml").read_bytes())
    if configured_env:
        monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path / "missing"))
    else:
        monkeypatch.delenv("MOONGAS_COLLECTION_ROOTDIR", raising=False)
    monkeypatch.setattr(sys, "argv", ["compare-track-yaml-with-media-file", flag, str(directory)])
    with caplog.at_level(logging.INFO):
        assert compare.main() == 0
    assert caplog.text.count("Track YAML matches") == 2

    monkeypatch.setattr(sys, "argv", ["compare-track-yaml-with-media-file", flag, str(directory), "-q"])
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert compare.main() == 0
    assert caplog.text == ""


@pytest.mark.parametrize("use_file", [False, True])
def test_cli_rejects_invalid_directory(
    track: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, use_file: bool
) -> None:
    directory = track if use_file else tmp_path / "missing"
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["compare-track-yaml-with-media-file", "-d", str(directory)])
    assert compare.main() == 1
    assert "Specified directory does not exist or is not a directory" in caplog.text


@pytest.mark.parametrize("missing_env", [False, True])
def test_cli_reports_invalid_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, missing_env: bool
) -> None:
    if missing_env:
        monkeypatch.delenv("MOONGAS_COLLECTION_ROOTDIR", raising=False)
    else:
        monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["compare-track-yaml-with-media-file", "-q"])
    assert compare.main() == 1
    assert caplog.records[-1].levelno == logging.ERROR
