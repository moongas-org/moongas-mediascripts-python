import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, COMM

from mediascripts.track.track_yaml import compare_track_yaml_with_media_file as compare
from mediascripts.track.track_yaml import merge_track_yaml_with_media_file as merge
from mediascripts.track.track_yaml.track_yaml_common import TAG_FIELDS


@pytest.mark.skipif(os.name != "posix", reason="Pseudo-terminal test requires POSIX")
@pytest.mark.parametrize(
    ("key", "result"),
    [(b"y", "y"), (b"m", "m"), (b"e", "e"), (b"s", "s"),
     (b"\r", "y"), (b" ", "m"), (b"\x03", "interrupted"), (b"\x04", "interrupted")],
)
def test_terminal_choice_requires_no_enter_and_restores_terminal(
    key: bytes, result: str
) -> None:
    import pty
    import termios
    import time

    master, slave = pty.openpty()
    original_settings = termios.tcgetattr(slave)
    code = (
        "from pathlib import Path\n"
        "from mediascripts.track.track_yaml.merge_track_yaml_with_media_file import prompt_resolution, prompt_custom_value\n"
        "try:\n"
        "    result = prompt_resolution(Path('Track.mp3'), 'title', 'YAML', ['ID3'])\n"
        "except (KeyboardInterrupt, EOFError):\n"
        "    result = 'interrupted'\n"
        "if result == 'e':\n"
        "    print('CUSTOM=' + prompt_custom_value('title'))\n"
        "print('RESULT=' + result)\n"
    )
    try:
        with subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as process:
            try:
                deadline = time.monotonic() + 10
                while termios.tcgetattr(slave)[3] & termios.ICANON:
                    assert process.poll() is None, "Prompt exited before reading a key"
                    assert time.monotonic() < deadline, "Prompt did not enable single-key input"
                    time.sleep(0.01)
                # No newline is sent for shortcuts. Space must not accept the
                # destructive default; the next valid key chooses media instead.
                os.write(master, key + (b"m" if key == b" " else b""))
                if key == b"e":
                    while not termios.tcgetattr(slave)[3] & termios.ICANON:
                        assert time.monotonic() < deadline, "Custom input did not restore line editing"
                        time.sleep(0.01)
                    os.write(master, b"Entered title\n")
                output, errors = process.communicate(timeout=10)
                assert process.returncode == 0, errors
                assert f"RESULT={result}" in output
                if key == b"e":
                    assert "CUSTOM=Entered title" in output
                assert termios.tcgetattr(slave) == original_settings
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
    finally:
        os.close(master)
        os.close(slave)


@pytest.fixture
def track(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("COPY_MEDIALIB_EXCLUDE_KEYWORDS", raising=False)
    path = tmp_path / "Artist" / "Album [1995]" / "Track.mp3"
    path.parent.mkdir(parents=True)
    tags = EasyID3()
    for key, value in {
        "title": "Track", "artist": "Artist", "albumartist": "Artist",
        "album": "Album", "genre": "Rock", "date": "1995-03-27", "tracknumber": "1/10",
    }.items():
        tags[key] = value
    tags.save(path)
    id3 = ID3(path)
    id3.add(COMM(encoding=3, lang="eng", desc="note", text=["Keep this comment"]))
    id3.save(path)
    path.with_suffix(".yml").write_text(
        "# Keep this comment\ntrackData:\n"
        '  title: "Track" # inline comment\n'
        "  artist: Artist\n  albumartist: Artist\n  album: Album\n"
        "  genre: Rock\n  year: 1995\n"
        "trackIdData:\n"
        "  streamingData:\n"
        "    - streamingService: spotify\n"
        "      trackUrl: https://open.spotify.com/track/keep-me\n"
        "      trackId: keep-me\n"
        "      apiData: null\n"
        "customField: keep-this-too\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("field", list(TAG_FIELDS))
@pytest.mark.parametrize("choice", ["y", "m", ""])
def test_accept_either_source_for_each_field(
    track: Path, monkeypatch: pytest.MonkeyPatch, field: str, choice: str
) -> None:
    tag = TAG_FIELDS[field]
    tags = EasyID3(track)
    tags[tag] = "2001-06-12" if field == "year" else "Different"
    tags.save(track)
    before_media = track.read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt: choice)

    assert merge.merge_track(track)
    assert compare.compare_track(track, quiet=True)
    assert EasyID3(track)["tracknumber"] == ["1/10"]
    assert ID3(track).getall("COMM")[0].text == ["Keep this comment"]
    yaml_text = track.with_suffix(".yml").read_text(encoding="utf-8")
    assert "# Keep this comment" in yaml_text
    assert "# inline comment" in yaml_text
    document = yaml.safe_load(yaml_text)
    assert document["customField"] == "keep-this-too"
    assert document["trackIdData"]["streamingData"][0]["trackId"] == "keep-me"
    assert len(document["mediaFileData"]) == 1
    media_file = document["mediaFileData"][0]
    assert media_file["path"] == str(track)
    assert media_file["albumPath"] == str(track.parent)
    assert media_file["artistPath"] == str(track.parent.parent)
    assert media_file["size"] == track.stat().st_size
    assert media_file["format"] == "mp3"
    expected_media_values = {
        "title": "Track",
        "artist": "Artist",
        "albumartist": "Artist",
        "album": "Album",
        "genre": "Rock",
        "year": 1995,
    }
    if choice == "m":
        expected_media_values[field] = 2001 if field == "year" else "Different"
    for metadata_field, expected_value in expected_media_values.items():
        assert media_file[metadata_field] == expected_value
    assert "modTime" in media_file
    if choice in ("y", ""):
        assert document["trackData"][field] == expected_media_values[field]
    else:
        assert track.read_bytes() == before_media
        assert document["trackData"][field] == (2001 if field == "year" else "Different")
    assert not list(track.parent.glob(".*.tmp"))


@pytest.mark.parametrize("quiet", [False, True])
def test_accept_yaml_logs_copy_and_confirms_persisted_id3_tag(
    track: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, quiet: bool
) -> None:
    tags = EasyID3(track)
    tags["title"] = "Different title"
    tags.save(track)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    with caplog.at_level(logging.INFO):
        assert merge.merge_track(track, quiet=quiet)

    assert "Copying Track YAML title='Track' to MP3 ID3 title (previous=['Different title'])" in caplog.text
    assert "Saved MP3 ID3 title='Track' from Track YAML" in caplog.text
    assert EasyID3(track)["title"] == ["Track"]
    document = yaml.safe_load(track.with_suffix(".yml").read_text(encoding="utf-8"))
    assert document["trackData"]["title"] == "Track"
    assert document["mediaFileData"][0]["path"] == str(track)


@pytest.mark.parametrize("field", list(TAG_FIELDS))
def test_custom_value_is_saved_to_both_files(
    track: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, field: str
) -> None:
    tag = TAG_FIELDS[field]
    tags = EasyID3(track)
    tags[tag] = "2001" if field == "year" else "Different"
    tags.save(track)
    custom = "2020" if field == "year" else "Custom value"
    choices = iter(["E", "", "bad-year", " 2020 "] if field == "year" else ["e", " ", f" {custom} "])
    monkeypatch.setattr("builtins.input", lambda prompt: next(choices))
    with caplog.at_level(logging.INFO):
        assert merge.merge_track(track, quiet=True)

    assert EasyID3(track)[tag] == [custom]
    document = yaml.safe_load(track.with_suffix(".yml").read_text(encoding="utf-8"))
    assert document["trackData"][field] == (2020 if field == "year" else custom)
    assert document["customField"] == "keep-this-too"
    assert document["trackIdData"]["streamingData"][0]["trackId"] == "keep-me"
    assert "# inline comment" in track.with_suffix(".yml").read_text(encoding="utf-8")
    assert EasyID3(track)["tracknumber"] == ["1/10"]
    assert compare.compare_track(track, quiet=True)
    assert f"Saved custom {field}=" in caplog.text


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_custom_input_interruption_does_not_change_files(
    track: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException]
) -> None:
    tags = EasyID3(track)
    tags["title"] = "Different"
    tags.save(track)
    before = (track.read_bytes(), track.with_suffix(".yml").read_bytes())

    def choose(prompt: str) -> str:
        if prompt.startswith("Enter value for"):
            raise interruption()
        return "e"

    monkeypatch.setattr("builtins.input", choose)
    monkeypatch.setattr(sys, "argv", ["merge-track-yaml-with-media-file", "-d", str(tmp_path)])
    assert merge.main() == 130
    assert (track.read_bytes(), track.with_suffix(".yml").read_bytes()) == before


@pytest.mark.parametrize("choice", ["s"])
def test_skip_preserves_conflicting_metadata_but_adds_media_file_data(
    track: Path, monkeypatch: pytest.MonkeyPatch, choice: str
) -> None:
    tags = EasyID3(track)
    tags["title"] = "Different"
    tags.save(track)
    before_media = track.read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt: choice)
    assert not merge.merge_track(track)
    assert track.read_bytes() == before_media
    assert yaml.safe_load(track.with_suffix(".yml").read_text(encoding="utf-8"))["trackData"]["title"] == "Track"
    assert yaml.safe_load(track.with_suffix(".yml").read_text(encoding="utf-8"))["mediaFileData"][0]["path"] == str(track)


def test_invalid_input_reprompts_and_uppercase_shortcut_works(
    track: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tags = EasyID3(track)
    tags["title"] = "Different"
    tags.save(track)
    choices = iter(["invalid", "Y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(choices))
    assert merge.merge_track(track)
    output = capsys.readouterr().out
    assert "Press y, m, e, or s" in output
    assert "Track YAML = 'Track'" in output
    assert "Media File Tags = ['Different']" in output


@pytest.mark.parametrize("values", [[], ["One", "Two"], [""]])
def test_missing_or_multiple_media_values_cannot_be_copied_to_scalar_yaml(
    track: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    values: list[str]
) -> None:
    tags = EasyID3(track)
    if values:
        tags["title"] = values
    else:
        del tags["title"]
    tags.save(track)
    choices = iter(["m", "s"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(choices))
    assert not merge.merge_track(track)
    assert "Cannot copy missing/multiple" in caplog.text
    document = yaml.safe_load(track.with_suffix(".yml").read_text(encoding="utf-8"))
    assert document["trackData"]["title"] == "Track"
    assert document["mediaFileData"][0]["path"] == str(track)


def test_quiet_matching_track_adds_media_file_data_without_prompt(
    track: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Unexpected prompt"))
    before_media = track.read_bytes()
    with caplog.at_level(logging.INFO):
        assert merge.merge_track(track, quiet=True)
    assert "Added missing MediaFileData fields" in caplog.text
    assert track.read_bytes() == before_media
    document = yaml.safe_load(track.with_suffix(".yml").read_text(encoding="utf-8"))
    assert document["mediaFileData"][0]["format"] == "mp3"


def test_existing_media_file_data_is_completed_without_overwriting_values(
    track: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = track.with_suffix(".yml")
    original = yaml_path.read_text(encoding="utf-8")
    yaml_path.write_text(
        original.replace(
            "customField: keep-this-too\n",
            "mediaFileData:\n"
            "  - path: preserved-path.mp3\n"
            "    format: preserved-format\n"
            "    fpcalcData:\n"
            "      fingerprint: preserved-fingerprint\n"
            "customField: keep-this-too\n",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Unexpected prompt"))

    assert merge.merge_track(track, quiet=True)

    media_files = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))["mediaFileData"]
    assert len(media_files) == 1
    media_file = media_files[0]
    assert media_file["path"] == "preserved-path.mp3"
    assert media_file["format"] == "preserved-format"
    assert media_file["fpcalcData"]["fingerprint"] == "preserved-fingerprint"
    assert media_file["albumPath"] == str(track.parent)
    assert media_file["artistPath"] == str(track.parent.parent)
    assert media_file["size"] == track.stat().st_size


def test_multiple_media_file_entries_are_rejected_without_data_loss(
    track: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    yaml_path = track.with_suffix(".yml")
    original = yaml_path.read_text(encoding="utf-8")
    yaml_path.write_text(
        original.replace(
            "customField: keep-this-too\n",
            "mediaFileData:\n  - path: first.mp3\n  - path: second.mp3\n"
            "customField: keep-this-too\n",
        ),
        encoding="utf-8",
    )
    before = yaml_path.read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Unexpected prompt"))

    assert not merge.merge_track(track, quiet=True)

    assert "Expected at most one mediaFileData entry per track" in caplog.text
    assert yaml_path.read_bytes() == before


@pytest.mark.parametrize("flag", ["-d", "--dir"])
def test_merge_cli_recurses_and_quiet_does_not_hide_conflicts(
    track: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    monkeypatch.delenv("MOONGAS_COLLECTION_ROOTDIR", raising=False)
    tags = EasyID3(track)
    tags["title"] = "Different"
    tags.save(track)
    monkeypatch.setattr(sys, "argv", ["merge-track-yaml-with-media-file", flag, str(tmp_path), "-q"])
    monkeypatch.setattr("builtins.input", lambda prompt: "m")
    assert merge.main() == 0
    assert "title: Track YAML" in capsys.readouterr().out
    assert compare.compare_track(track, quiet=True)


def test_excluded_artist_does_not_need_yaml_or_tags(
    track: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COPY_MEDIALIB_EXCLUDE_KEYWORDS", "Artist")
    track.with_suffix(".yml").unlink()
    track.write_bytes(b"invalid media")
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Unexpected prompt"))
    assert merge.merge_track(track)


def test_missing_yaml_reports_error_without_prompt(
    track: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    track.with_suffix(".yml").unlink()
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Unexpected prompt"))
    assert not merge.merge_track(track)
    assert "Could not merge" in caplog.text


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_interruption_keeps_previous_decisions_and_does_not_change_current_field(
    track: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException]
) -> None:
    tags = EasyID3(track)
    tags["title"] = "Different title"
    tags["album"] = "Different album"
    tags.save(track)
    count = 0

    def choose(prompt: str) -> str:
        nonlocal count
        count += 1
        if count == 1:
            return "y"
        raise interruption()

    monkeypatch.setattr("builtins.input", choose)
    monkeypatch.setattr(sys, "argv", ["merge-track-yaml-with-media-file", "-d", str(tmp_path)])
    assert merge.main() == 130
    assert EasyID3(track)["title"] == ["Track"]
    assert EasyID3(track)["album"] == ["Different album"]


def test_failed_yaml_save_preserves_original_and_cleans_temporary_file(
    track: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tags = EasyID3(track)
    tags["title"] = "Different"
    tags.save(track)
    before = track.with_suffix(".yml").read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt: "m")

    def fail_replace(source: Path, destination: Path) -> None:
        raise PermissionError("Cannot replace YAML")

    monkeypatch.setattr("mediascripts.track.track_yaml.track_yaml_common.os.replace", fail_replace)
    assert not merge.merge_track(track)
    assert "Cannot replace YAML" in caplog.text
    assert track.with_suffix(".yml").read_bytes() == before
    assert not list(track.parent.glob(".*.tmp"))


def test_mixed_choices_in_real_cli_preserve_skipped_field(
    track: Path, tmp_path: Path
) -> None:
    tags = EasyID3(track)
    tags["title"] = "Different title"
    tags["album"] = "Different album"
    tags["genre"] = "Different genre"
    tags.save(track)
    result = subprocess.run(
        [
            sys.executable, "-m",
            "mediascripts.track.track_yaml.merge_track_yaml_with_media_file",
            "-d", str(tmp_path), "-q",
        ],
        input="y\nm\ns\n", capture_output=True, text=True, check=False,
        env={key: value for key, value in os.environ.items() if key != "COPY_MEDIALIB_EXCLUDE_KEYWORDS"},
    )
    assert result.returncode == 1, result.stderr
    assert "[y] Accept Track YAML" in result.stdout
    assert "[m] Accept Media File Tags" in result.stdout
    assert "[s] Skip" in result.stdout
    assert EasyID3(track)["title"] == ["Track"]
    assert EasyID3(track)["album"] == ["Different album"]
    assert EasyID3(track)["genre"] == ["Different genre"]
    data = yaml.safe_load(track.with_suffix(".yml").read_text(encoding="utf-8"))["trackData"]
    assert data["album"] == "Different album"
    assert data["genre"] == "Rock"


def test_collection_environment_is_used_without_directory_argument(
    track: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(track.parent.parent.parent))
    data = track.parent.parent.parent / "data"
    (data / "Music").mkdir(parents=True)
    (data / "Music" / "Artist").symlink_to(track.parent.parent, target_is_directory=True)
    monkeypatch.setattr(sys, "argv", ["merge-track-yaml-with-media-file", "-q"])
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Unexpected prompt"))
    assert merge.main() == 0


def test_failed_media_save_reports_error(
    track: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tags = EasyID3(track)
    tags["title"] = "Different"
    tags.save(track)
    before = track.read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    def fail_save(*args: object, **kwargs: object) -> None:
        raise PermissionError("Cannot save media")

    monkeypatch.setattr(EasyID3, "save", fail_save)
    assert not merge.merge_track(track)
    assert "Cannot save media" in caplog.text
    assert track.read_bytes() == before
