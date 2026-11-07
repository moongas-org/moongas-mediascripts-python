import base64
import logging
import shutil
import subprocess
from io import BytesIO
from pathlib import Path

import mutagen
import pytest
from mutagen.asf import ASF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import COMM, ID3, TCOM, TCON, TCOP, TDRC, TIT2, TPE1, TRCK
from mutagen.mp4 import MP4
from PIL import Image
from ruamel.yaml import YAML

from mediascripts.mediafile import auto_tag_media_file as tagger


def image_bytes(format: str) -> bytes:
    with BytesIO() as output:
        Image.new("RGB", (2, 2), "red").save(output, format=format)
        return output.getvalue()


JPEG = image_bytes("JPEG")
WEBP = image_bytes("WEBP")


@pytest.fixture
def track(tmp_path: Path) -> Path:
    path = tmp_path / "Artist_Name" / "Album_Name [2024]" / "05 - BIG 5.mp3"
    path.parent.mkdir(parents=True)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Old title"]))
    tags.add(TPE1(encoding=3, text=["Old artist"]))
    tags.add(TRCK(encoding=3, text=["5/12"]))
    tags.add(COMM(encoding=3, lang="eng", desc="note", text=["Keep this"]))
    tags.save(path)
    return path


def write_yaml(track: Path, data: object, suffix: str = ".yml", key: str = "trackData") -> Path:
    path = track.with_suffix(suffix)
    with path.open("w") as stream:
        YAML().dump({key: data}, stream)
    return path


def test_context_and_preservation(track: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        assert tagger.auto_tag_media_file(track)
    tags = ID3(track)
    for key, value in {
        "TPE1": "Artist.Name", "TPE2": "Artist.Name", "TALB": "Album.Name",
        "TIT2": "BIG 5", "TDRC": "2024", "TRCK": "5/12",
    }.items():
        assert str(tags[key]) == value
    assert tags.getall("COMM")[0].text == ["Keep this"]
    assert "TCON" not in tags
    assert "Set title: ['Old title'] -> 'BIG 5'" in caplog.text
    original = track.read_bytes()
    assert not tagger.auto_tag_media_file(track)
    assert track.read_bytes() == original


def test_post_update_output_includes_all_saved_tags(
    track: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[Path] = []
    load_media = tagger.load_media

    def record_load(path: Path) -> ID3 | mutagen.FileType:
        loads.append(path)
        return load_media(path)

    monkeypatch.setattr(tagger, "load_media", record_load)
    assert tagger.main(["--file", str(track), "--title", "Updated title", "-v"]) == 0
    output = capsys.readouterr().out
    assert loads == [track.absolute(), track.absolute()]
    assert "Post-update tags" in output
    assert ID3(track).pprint() in output
    assert "TIT2=Updated title" in output
    assert "Keep this" in output
    assert "Old title" not in output
    assert tagger.main(["--file", str(track), "--title", "Updated title", "--verbose"]) == 0
    assert ID3(track).pprint() in capsys.readouterr().out


def test_dry_run_output_shows_proposed_tags(track: Path, capsys: pytest.CaptureFixture[str]) -> None:
    original = track.read_bytes()
    assert tagger.main(["--file", str(track), "--title", "Proposed title", "-n", "-v"]) == 0
    output = capsys.readouterr().out
    assert "Proposed tags (dry-run; not saved)" in output
    assert "TIT2=Proposed title" in output
    assert "Post-update tags" not in output
    assert track.read_bytes() == original


@pytest.mark.parametrize("dry_run", [False, True])
def test_default_output_omits_tag_listing(
    track: Path, dry_run: bool, capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[Path] = []
    load_media = tagger.load_media

    def record_load(path: Path) -> ID3 | mutagen.FileType:
        loads.append(path)
        return load_media(path)

    monkeypatch.setattr(tagger, "load_media", record_load)
    args = ["--file", str(track)]
    if dry_run:
        args.append("-n")
    with caplog.at_level(logging.INFO):
        assert tagger.main(args) == 0
    output = capsys.readouterr().out
    assert ("Proposed values (dry-run; not saved)" if dry_run else "Final values") in output
    assert "  title: ['BIG 5']" in output
    assert "  comment: ['Keep this']" in output
    assert "  tracknumber: ['5/12']" in output
    for field in ("genre", "discnumber", "composer", "copyright", "cover"):
        assert f"  {field}: (not set)" in output
    assert "Post-update tags" not in output
    assert "TIT2=" not in output
    assert loads == [track.absolute()] * (1 if dry_run else 2)
    assert ("Would set title" if dry_run else "Set title") in caplog.text


def test_final_summary_reports_cleared_values_and_cover(
    track: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    (track.parent / "cover.jpg").write_bytes(JPEG)
    assert tagger.main(["--file", str(track), "--comment", "", "--track", "1", "--disc", "1/2"]) == 0
    output = capsys.readouterr().out
    assert "  comment: (not set)" in output
    assert "  tracknumber: ['1']" in output
    assert "  discnumber: ['1/2']" in output
    assert "  cover: present" in output
    assert "Keep this" not in output
    assert not tagger.auto_tag_media_file(track, comment="", tracknumber="1", discnumber="1/2")
    assert "Final values" in capsys.readouterr().out


@pytest.mark.parametrize("suffix,key", [(".yml", "trackData"), (".yaml", "track_data")])
def test_yaml_and_cli_precedence(track: Path, suffix: str, key: str) -> None:
    yaml_path = write_yaml(track, {
        "artist": "YAML_artist", "albumartist": "YAML album artist", "album": "YAML album",
        "title": "YAML_title", "genre": "Hip-Hop", "year": 1999,
    }, suffix, key)
    original_yaml = yaml_path.read_bytes()
    tagger.auto_tag_media_file(track)
    tags = ID3(track)
    assert str(tags["TALB"]) == "YAML album"
    assert str(tags["TPE1"]) == "YAML_artist"
    assert str(tags["TPE2"]) == "YAML album artist"
    assert str(tags["TDRC"]) == "1999"
    tagger.auto_tag_media_file(
        track, artist="CLI_artist", albumartist="CLI album artist", album="CLI album",
        title="CLI_title", genre="Jazz", year="2001",
    )
    tags = ID3(track)
    assert str(tags["TPE1"]) == "CLI_artist"
    assert str(tags["TPE2"]) == "CLI album artist"
    assert str(tags["TALB"]) == "CLI album"
    assert str(tags["TIT2"]) == "CLI_title"
    assert str(tags["TCON"]) == "Jazz"
    assert str(tags["TDRC"]) == "2001"
    assert yaml_path.read_bytes() == original_yaml


def test_partial_yaml_falls_back(track: Path) -> None:
    write_yaml(track, {"title": "YAML", "artist": None, "album": "", "albumartist": "  ", "year": 0})
    values = tagger.resolve_values(track, {})
    assert values == {
        "artist": "Artist.Name", "albumartist": "Artist.Name", "album": "Album.Name",
        "title": "YAML", "year": "2024",
    }


def test_missing_contextual_year_does_not_clear_existing_date(track: Path) -> None:
    tags = ID3(track)
    tags.add(TDRC(encoding=3, text=["1997-04-03"]))
    tags.save(track)
    destination = track.parent.with_name("Album without year")
    track.parent.rename(destination)
    track = destination / track.name
    tagger.auto_tag_media_file(track)
    assert str(ID3(track)["TDRC"]) == "1997-04-03"


def test_save_failure_is_logged_without_claiming_changes(
    track: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_save(*args: object, **kwargs: object) -> None:
        raise OSError("permission denied")

    before = track.read_bytes()
    monkeypatch.setattr(ID3, "save", fail_save)
    with caplog.at_level(logging.INFO):
        assert tagger.main(["--file", str(track)]) == 1
    assert "permission denied" in caplog.text
    assert "Set " not in caplog.text
    assert track.read_bytes() == before


@pytest.mark.parametrize("filename,title", [
    ("05 - BIG 5.mp3", "BIG 5"), ("05. BIG 5.mp3", "BIG 5"),
    ("05_ BIG 5.mp3", "BIG 5"), ("01.05 - BIG 5.mp3", "BIG 5"),
    ("BIG 5.mp3", "BIG 5"), ("1984.mp3", "1984"), ("Jay-Z - Song.mp3", "Jay-Z - Song"),
])
def test_filename_clues(track: Path, filename: str, title: str) -> None:
    assert tagger.resolve_values(track.with_name(filename), {})["title"] == title


@pytest.mark.parametrize("filename,expected", [
    ("05 - BIG 5.mp3", "5"), ("5 BIG 5.mp3", "5"), ("01 - Song.mp3", "1"),
    ("99 Song.mp3", "99"), ("0005 Song.mp3", "5"), ("5Song.mp3", "5"),
    ("00 Song.mp3", None), ("100 Song.mp3", None), ("999 Song.mp3", None),
    ("Song.mp3", None), ("-5 Song.mp3", None),
])
@pytest.mark.parametrize("existing", [None, "4", "4/12"])
def test_filename_track_numbers(
    track: Path, filename: str, expected: str | None, existing: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    renamed = track.with_name(filename)
    track.rename(renamed)
    tags = ID3(renamed)
    tags.delall("TRCK")
    if existing is not None:
        tags.add(TRCK(encoding=3, text=[existing]))
    tags.save(renamed)
    with caplog.at_level(logging.INFO):
        tagger.auto_tag_media_file(renamed)
    actual = ID3(renamed).getall("TRCK")
    if expected is None:
        assert [str(frame) for frame in actual] == ([existing] if existing else [])
        assert "Set tracknumber" not in caplog.text
    else:
        total = "/12" if existing == "4/12" else ""
        assert [str(frame) for frame in actual] == [expected + total]
        assert "Set tracknumber" in caplog.text
    before = renamed.read_bytes()
    assert not tagger.auto_tag_media_file(renamed)
    assert renamed.read_bytes() == before


def test_track_number_dry_run(track: Path, caplog: pytest.LogCaptureFixture) -> None:
    tags = ID3(track)
    tags.add(TRCK(encoding=3, text=["4/12"]))
    tags.save(track)
    original = track.read_bytes()
    with caplog.at_level(logging.INFO):
        assert tagger.main(["--file", str(track), "-n"]) == 0
    assert "Would set tracknumber: ['4/12'] -> '5/12'" in caplog.text
    assert track.read_bytes() == original


@pytest.mark.parametrize("filename", ["05 - BIG 5.mp3", "Song.mp3", "100 Song.mp3"])
@pytest.mark.parametrize("number", ["1", "07", "99"])
def test_explicit_track_overrides_filename(track: Path, filename: str, number: str) -> None:
    renamed = track.with_name(filename)
    track.rename(renamed)
    assert tagger.main(["--file", str(renamed), "--track", number]) == 0
    assert str(ID3(renamed)["TRCK"]) == str(int(number))
    original = renamed.read_bytes()
    assert tagger.main(["--file", str(renamed), "--track", number]) == 0
    assert renamed.read_bytes() == original


@pytest.mark.parametrize("number", ["0", "100", "-1", "5.5", "abc", "5/", "/12", "5/0", "5/65536", "5/12/13"])
def test_invalid_explicit_track_does_not_save(
    track: Path, number: str, caplog: pytest.LogCaptureFixture,
) -> None:
    original = track.read_bytes()
    assert tagger.main(["--file", str(track), "--track", number]) == 1
    assert "Could not tag media file" in caplog.text
    assert track.read_bytes() == original


def test_explicit_track_dry_run(track: Path, caplog: pytest.LogCaptureFixture) -> None:
    original = track.read_bytes()
    with caplog.at_level(logging.INFO):
        assert tagger.main(["--file", str(track), "--track", "7", "-n"]) == 0
    assert "Would set tracknumber: ['5/12'] -> '7'" in caplog.text
    assert track.read_bytes() == original


@pytest.mark.parametrize("field,frame_type", [("comment", COMM), ("composer", TCOM), ("copyright", TCOP)])
def test_optional_id3_fields(
    track: Path, field: str, frame_type: type,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tags = ID3(track)
    if field == "comment":
        tags.add(COMM(encoding=3, lang="fra", desc="other", text=["Autre"]))
    else:
        tags.add(frame_type(encoding=3, text=["Original"]))
    tags.save(track)
    tagger.auto_tag_media_file(track)
    original_frames = ID3(track).getall(frame_type.__name__)
    assert original_frames
    before = track.read_bytes()
    with caplog.at_level(logging.INFO):
        assert tagger.main(["--file", str(track), f"--{field}", "New value", "-n"]) == 0
    assert f"Would set {field}" in caplog.text
    assert track.read_bytes() == before
    assert tagger.main(["--file", str(track), f"--{field}", "New value"]) == 0
    frames = ID3(track).getall(frame_type.__name__)
    assert len(frames) == 1
    assert frames[0].text == ["New value"]
    if field == "comment":
        assert frames[0].lang == "eng"
        assert frames[0].desc == ""
    saved = track.read_bytes()
    assert not tagger.auto_tag_media_file(track, **{field: "New value"})
    assert track.read_bytes() == saved
    assert tagger.main(["--file", str(track), f"--{field}", "", "-n"]) == 0
    assert track.read_bytes() == saved
    assert tagger.main(["--file", str(track), f"--{field}", ""]) == 0
    assert not ID3(track).getall(frame_type.__name__)
    assert not tagger.auto_tag_media_file(track, **{field: ""})
    for other in {"COMM", "TCOM", "TCOP"} - {frame_type.__name__}:
        assert ID3(track).getall(other) == tags.getall(other)


def test_dry_run_changes_nothing(track: Path, caplog: pytest.LogCaptureFixture) -> None:
    write_yaml(track, {"genre": "Jazz"})
    (track.parent / "cover.jpg").write_bytes(JPEG)
    original = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in track.parent.iterdir()}
    with caplog.at_level(logging.INFO):
        assert tagger.main(["--file", str(track), "-n", "--album", "CLI album"]) == 0
    assert "Would set album" in caplog.text
    assert "Would set cover" in caplog.text
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in track.parent.iterdir()} == original


def test_cover_preference_and_preservation(track: Path) -> None:
    (track.parent / "cover.webp").write_bytes(WEBP * 100)
    (track.parent / "cover.jpg").write_bytes(JPEG)
    largest = JPEG + b"larger"
    (track.parent / "COVER.JPG").write_bytes(largest)
    tagger.auto_tag_media_file(track)
    assert ID3(track).getall("APIC")[0].data == largest
    original = track.read_bytes()
    assert not tagger.auto_tag_media_file(track, cover=track.parent / "does-not-exist.jpg")
    assert track.read_bytes() == original


@pytest.mark.parametrize("name,data,mime", [
    ("cover.webp", WEBP, "image/webp"), ("custom.jpg", JPEG, "image/jpeg"),
])
def test_webp_and_explicit_cover(track: Path, name: str, data: bytes, mime: str) -> None:
    image = track.parent / name
    image.write_bytes(data)
    tagger.auto_tag_media_file(track, cover=image if name.startswith("custom") else None)
    cover = ID3(track).getall("APIC")[0]
    assert cover.data == data
    assert cover.mime == mime


@pytest.mark.parametrize("data", [
    {"year": "bad"}, {"title": ["not", "scalar"]},
    {"album": True}, ["not a mapping"],
])
def test_bad_yaml_is_not_silently_ignored(track: Path, data: object, caplog: pytest.LogCaptureFixture) -> None:
    write_yaml(track, data)
    before = track.read_bytes()
    assert tagger.main(["--file", str(track)]) == 1
    assert "Could not tag media file" in caplog.text
    assert track.read_bytes() == before


def test_malformed_yaml(track: Path) -> None:
    track.with_suffix(".yml").write_text("trackData: [")
    before = track.read_bytes()
    assert tagger.main(["--file", str(track)]) == 1
    assert track.read_bytes() == before


def test_multiple_yaml_files_fail(track: Path) -> None:
    write_yaml(track, {"title": "one"}, ".yml")
    write_yaml(track, {"title": "two"}, ".yaml")
    with pytest.raises(ValueError, match="Multiple track YAML"):
        tagger.auto_tag_media_file(track)


def test_invalid_cover_prevents_all_changes(track: Path) -> None:
    cover = track.parent / "cover.jpg"
    cover.write_bytes(b"not an image")
    original = track.read_bytes()
    assert tagger.main(["--file", str(track)]) == 1
    assert track.read_bytes() == original


def test_missing_id3_header(track: Path) -> None:
    track.write_bytes(b"\xff\xfb" + b"\x00" * 100)
    tagger.auto_tag_media_file(track)
    assert str(ID3(track)["TIT2"]) == "BIG 5"


@pytest.mark.parametrize("args", [
    ["--year", "bad"], ["--album", ""], ["--cover", "/missing/cover.jpg"],
])
def test_bad_cli_values(track: Path, args: list[str]) -> None:
    before = track.read_bytes()
    assert tagger.main(["--file", str(track), *args]) == 1
    assert track.read_bytes() == before


@pytest.mark.parametrize("source", ["tags", "yaml"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_genre_is_cleared_with_warning(
    track: Path, source: str, dry_run: bool,
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    tags = ID3(track)
    tags.add(TCON(encoding=3, text=["Jazz", "made up"] if source == "tags" else ["Jazz"]))
    tags.save(track)
    args = ["--file", str(track)]
    if source == "yaml":
        write_yaml(track, {"genre": "made up"})
    if dry_run:
        args.append("-n")
    original = track.read_bytes()
    assert tagger.main(args) == 0
    assert "Invalid genre values" in caplog.text
    assert "made up" in caplog.text
    assert "  genre: (not set)" in capsys.readouterr().out
    if dry_run:
        assert track.read_bytes() == original
    else:
        assert not ID3(track).getall("TCON")


@pytest.mark.parametrize("genre", ["made up", " Jazz", "Jazz ", ""])
@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_cli_genre_exits_before_processing(
    track: Path, genre: str, dry_run: bool, caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> bool:
        pytest.fail("Invalid CLI genre must be rejected before processing the file")

    monkeypatch.setattr(tagger, "auto_tag_media_file", fail_if_called)
    original = track.read_bytes()
    args = ["--file", str(track), "--genre", genre]
    if dry_run:
        args.append("-n")
    assert tagger.main(args) == 1
    assert any(record.levelno == logging.ERROR and "Invalid --genre" in record.message for record in caplog.records)
    assert "case-insensitive" in caplog.text
    assert track.read_bytes() == original
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("genre", list(tagger.Genre))
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("omit_hyphens", [False, True])
def test_cli_genre_uses_canonical_enum_case(
    track: Path, genre: tagger.Genre, dry_run: bool, omit_hyphens: bool, capsys: pytest.CaptureFixture[str],
) -> None:
    original = track.read_bytes()
    value = genre.value.swapcase()
    if omit_hyphens:
        value = value.replace("-", "")
    args = ["--file", str(track), "--genre", value]
    if dry_run:
        args.append("-n")
    assert tagger.main(args) == 0
    assert f"  genre: {[genre.value]!r}" in capsys.readouterr().out
    if dry_run:
        assert track.read_bytes() == original
    else:
        assert tagger.text_values(ID3(track), "genre") == [genre.value]


def test_valid_genre_override_wins_over_invalid_yaml_and_tags(track: Path) -> None:
    tags = ID3(track)
    tags.add(TCON(encoding=3, text=["made up"]))
    tags.save(track)
    write_yaml(track, {"genre": "also invalid"})
    assert tagger.main(["--file", str(track), "--genre", "Jazz"]) == 0
    assert str(ID3(track)["TCON"]) == "Jazz"


@pytest.mark.parametrize("genres", [[], ["Jazz"], ["Jazz", "Hip-Hop"]])
def test_valid_or_absent_existing_genre_preserved(track: Path, genres: list[str]) -> None:
    tags = ID3(track)
    if genres:
        tags.add(TCON(encoding=3, text=genres))
    tags.save(track)
    tagger.auto_tag_media_file(track)
    assert tagger.text_values(ID3(track), "genre") == genres


def genre_neighbor(path: Path, genre: str, *, yaml_genre: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tags = ID3()
    tags.add(TCON(encoding=3, text=[genre]))
    tags.save(path)
    if yaml_genre is not None:
        write_yaml(path, {"genre": yaml_genre})


@pytest.mark.parametrize("dry_run", [False, True])
def test_artist_genre_inference_counts_tracks_once(
    track: Path, dry_run: bool, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    genre_neighbor(track.with_name("01.mp3"), "Hip-Hop", yaml_genre="Jazz")
    genre_neighbor(track.with_name("01.MP3"), "Hip-Hop")
    other = track.parent.parent / "Other [2000]"
    genre_neighbor(other / "02.mp3", "Jazz")
    genre_neighbor(other / "03.mp3", "Hip-Hop")
    genre_neighbor(other / "04.mp3", "made up")
    genre_neighbor(track.parent.parent.parent / "Other Artist" / "Album" / "01.mp3", "Hip-Hop")
    before = {p: p.read_bytes() for p in track.parent.parent.rglob("*") if p.is_file()}
    with caplog.at_level(logging.INFO):
        assert tagger.auto_tag_media_file(track, dry_run=dry_run)
    assert "  genre: ['Jazz']" in capsys.readouterr().out
    assert "Inferred artist genre 'Jazz'" in caplog.text
    assert "'Jazz': 2" in caplog.text
    for p, contents in before.items():
        if dry_run or p != track:
            assert p.read_bytes() == contents
    if not dry_run:
        assert str(ID3(track)["TCON"]) == "Jazz"


@pytest.mark.parametrize("source", ["cli", "yaml", "tags"])
def test_genre_inference_preserves_higher_priority_genre(
    track: Path, source: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_scan(path: Path) -> str:
        pytest.fail("A valid target genre must prevent neighbor scanning")

    monkeypatch.setattr(tagger, "infer_artist_genre", fail_scan)
    if source == "yaml":
        write_yaml(track, {"genre": "Jazz"})
    elif source == "tags":
        tags = ID3(track)
        tags.add(TCON(encoding=3, text=["Jazz"]))
        tags.save(track)
    tagger.auto_tag_media_file(track, **({"genre": "Jazz"} if source == "cli" else {}))
    assert str(ID3(track)["TCON"]) == "Jazz"


def test_genre_inference_invalid_target_and_unreadable_clues(
    track: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    tags = ID3(track)
    tags.add(TCON(encoding=3, text=["made up"]))
    tags.save(track)
    neighbor = track.with_name("01.mp3")
    genre_neighbor(neighbor, "Jazz")
    neighbor.with_suffix(".yml").write_text("trackData: [")
    tagger.auto_tag_media_file(track)
    assert str(ID3(track)["TCON"]) == "Jazz"
    assert "Could not read neighboring track YAML" in caplog.text


def test_artist_genre_tie_is_alphabetical_and_target_excluded(track: Path) -> None:
    genre_neighbor(track.with_name("01.mp3"), "Jazz")
    genre_neighbor(track.with_name("02.mp3"), "Hip-Hop")
    write_yaml(track, {"genre": "Jazz"})
    assert tagger.infer_artist_genre(track) == "Hip-Hop"


def test_missing_and_unrecognized_files(tmp_path: Path) -> None:
    assert tagger.main(["--file", str(tmp_path / "missing.mp3")]) == 1
    path = tmp_path / "file.txt"
    path.write_text("not media")
    assert tagger.main(["--file", str(path)]) == 1


@pytest.fixture
def native_track(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is required to generate native format fixtures")
    path = tmp_path / "Artist" / "Album [2003]" / f"05 - BIG 5.{request.param}"
    path.parent.mkdir(parents=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "0.1", str(path)],
        check=True, capture_output=True,
    )
    return path


@pytest.mark.parametrize("native_track", ["mp3", "wav", "flac", "ogg", "opus", "m4a", "mp4", "wma"], indirect=True)
def test_native_format_roundtrip(native_track: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (native_track.parent / "cover.jpg").write_bytes(JPEG)
    original = native_track.read_bytes()
    assert tagger.auto_tag_media_file(native_track, dry_run=True, genre="Jazz")
    assert native_track.read_bytes() == original
    assert tagger.auto_tag_media_file(native_track, genre="Jazz", verbose=True)
    output = capsys.readouterr().out
    assert "Post-update tags" in output
    assert "BIG 5" in output
    if native_track.suffix == ".flac":
        assert "PICTURE[1]: type=3, mime=image/jpeg" in output
        assert f"size={len(JPEG)} bytes" in output
    media = mutagen.File(native_track)
    assert media is not None
    if isinstance(media, MP4):
        assert media["trkn"] == [(5, 0)]
        assert media["\xa9nam"] == ["BIG 5"]
        assert bytes(media["covr"][0]) == JPEG
    elif isinstance(media, FLAC):
        assert media["tracknumber"] == ["5"]
        assert media["title"] == ["BIG 5"]
        assert media.pictures[0].data == JPEG
    elif isinstance(media, ASF):
        assert str(media["WM/TrackNumber"][0]) == "5"
        assert str(media["Title"][0]) == "BIG 5"
        assert media["WM/Picture"][0].value.endswith(JPEG)
    elif isinstance(media.tags, ID3):
        assert str(media.tags["TRCK"]) == "5"
        assert str(media.tags["TIT2"]) == "BIG 5"
        assert media.tags.getall("APIC")[0].data == JPEG
    else:
        assert media["tracknumber"] == ["5"]
        assert media["title"] == ["BIG 5"]
        assert Picture(base64.b64decode(media["metadata_block_picture"][0])).data == JPEG
    saved = native_track.read_bytes()
    assert not tagger.auto_tag_media_file(native_track, genre="Jazz")
    assert native_track.read_bytes() == saved


@pytest.mark.parametrize("native_track", ["m4a"], indirect=True)
def test_mp4_track_number_preserves_total(native_track: Path) -> None:
    media = MP4(native_track)
    media["trkn"] = [(4, 12)]
    media.save()
    before = native_track.read_bytes()
    assert tagger.auto_tag_media_file(native_track, dry_run=True)
    assert native_track.read_bytes() == before
    tagger.auto_tag_media_file(native_track)
    assert MP4(native_track)["trkn"] == [(5, 12)]
    assert not tagger.auto_tag_media_file(native_track)
    assert tagger.main(["--file", str(native_track), "--track", "7"]) == 0
    assert MP4(native_track)["trkn"] == [(7, 0)]


@pytest.mark.parametrize("native_track", ["mp3", "wav", "flac", "ogg", "opus", "m4a", "mp4", "wma"], indirect=True)
def test_invalid_native_genre_is_removed(native_track: Path, caplog: pytest.LogCaptureFixture) -> None:
    media = tagger.load_media(native_track)
    tagger.text_values(media, "genre", "made up genre")
    media.save(native_track)
    original = native_track.read_bytes()
    assert tagger.main(["--file", str(native_track), "-n"]) == 0
    assert native_track.read_bytes() == original
    assert "would unset" in caplog.text
    assert tagger.main(["--file", str(native_track)]) == 0
    assert tagger.text_values(tagger.load_media(native_track), "genre") == []


@pytest.mark.parametrize("native_track", ["mp3", "wav", "flac", "ogg", "opus", "m4a", "mp4", "wma"], indirect=True)
def test_explicit_positions_replace_totals_and_clear(native_track: Path) -> None:
    args = ["--file", str(native_track)]
    assert tagger.main([*args, "--track", "01/10", "--disc", "01/02"]) == 0
    media = tagger.load_media(native_track)
    assert tagger.text_values(media, "tracknumber") == ["1/10"]
    assert tagger.text_values(media, "discnumber") == ["1/2"]
    if isinstance(media, MP4):
        assert media["trkn"] == [(1, 10)]
        assert media["disk"] == [(1, 2)]
    elif isinstance(media, ID3):
        assert str(media["TRCK"]) == "1/10"
        assert str(media["TPOS"]) == "1/2"
    original = native_track.read_bytes()
    assert tagger.main([*args, "--track", "1", "--disc", "", "-n"]) == 0
    assert native_track.read_bytes() == original
    assert tagger.main([*args, "--track", "1", "--disc", ""]) == 0
    media = tagger.load_media(native_track)
    assert tagger.text_values(media, "tracknumber") == ["1"]
    assert tagger.text_values(media, "discnumber") == []
    if isinstance(media, MP4):
        assert media["trkn"] == [(1, 0)]
        assert "disk" not in media
    assert not tagger.auto_tag_media_file(native_track, tracknumber="1", discnumber="")
    assert tagger.main([*args, "--disc", "2"]) == 0
    assert tagger.text_values(tagger.load_media(native_track), "discnumber") == ["2"]
    assert tagger.main([*args, "--track", ""]) == 0
    media = tagger.load_media(native_track)
    assert tagger.text_values(media, "tracknumber") == []
    assert tagger.text_values(media, "discnumber") == ["2"]


@pytest.mark.parametrize("number", ["0", "100", "-1", "x", "1/", "/2", "1/0", "1/65536", "1/2/3"])
def test_invalid_disc_does_not_save(track: Path, number: str) -> None:
    original = track.read_bytes()
    assert tagger.main(["--file", str(track), "--disc", number]) == 1
    assert track.read_bytes() == original


@pytest.mark.parametrize("native_track", ["flac", "ogg", "opus"], indirect=True)
@pytest.mark.parametrize("track_value,disc_value", [("1", "1"), ("1/10", "1/2"), ("", "")])
def test_vorbis_separate_totals_are_removed(native_track: Path, track_value: str, disc_value: str) -> None:
    media = mutagen.File(native_track)
    assert media is not None
    media["tracknumber"] = ["4"]
    media["discnumber"] = ["2"]
    for key in ("tracktotal", "totaltracks", "disctotal", "totaldiscs"):
        media[key] = ["12"]
    media.save()
    assert tagger.main([
        "--file", str(native_track), "--track", track_value, "--disc", disc_value,
    ]) == 0
    media = mutagen.File(native_track)
    assert media is not None
    for key in ("tracktotal", "totaltracks", "disctotal", "totaldiscs"):
        assert key not in media
    assert media.tags.get("tracknumber", []) == ([track_value] if track_value else [])
    assert media.tags.get("discnumber", []) == ([disc_value] if disc_value else [])


@pytest.mark.parametrize("native_track", ["wav", "flac", "ogg", "opus", "m4a", "mp4", "wma"], indirect=True)
def test_optional_native_fields(native_track: Path) -> None:
    args = [
        "--file", str(native_track), "--comment", "Notes",
        "--composer", "Composer", "--copyright", "Copyright",
    ]
    assert tagger.main(args) == 0
    media = mutagen.File(native_track)
    assert media is not None
    if isinstance(media.tags, ID3):
        keys = ("COMM", "TCOM", "TCOP")
        for key, value in zip(keys, ("Notes", "Composer", "Copyright")):
            assert media.tags.getall(key)[0].text == [value]
    else:
        keys = (
            ("\xa9cmt", "\xa9wrt", "cprt") if isinstance(media, MP4)
            else ("Description", "WM/Composer", "Copyright") if isinstance(media, ASF)
            else ("comment", "composer", "copyright")
        )
        for key, value in zip(keys, ("Notes", "Composer", "Copyright")):
            assert str(media[key][0]) == value
    before = native_track.read_bytes()
    assert not tagger.auto_tag_media_file(native_track, comment="Notes", composer="Composer", copyright="Copyright")
    assert native_track.read_bytes() == before
    assert tagger.main([
        "--file", str(native_track), "--comment", "", "--composer", "", "--copyright", "",
    ]) == 0
    cleared = mutagen.File(native_track)
    assert cleared is not None
    for key in keys:
        if isinstance(cleared.tags, ID3):
            assert not cleared.tags.getall(key)
        else:
            assert key not in cleared
    assert not tagger.auto_tag_media_file(native_track, comment="", composer="", copyright="")


@pytest.mark.parametrize("native_track", ["m4a"], indirect=True)
def test_mp4_webp_is_converted_without_changing_source(native_track: Path) -> None:
    (native_track.parent / "cover.webp").write_bytes(WEBP)
    original = native_track.read_bytes()
    assert tagger.main(["--file", str(native_track), "-n"]) == 0
    assert native_track.read_bytes() == original
    assert tagger.main(["--file", str(native_track)]) == 0
    with Image.open(BytesIO(bytes(MP4(native_track)["covr"][0]))) as cover:
        assert cover.format == "JPEG"
        assert cover.size == (2, 2)
    assert (native_track.parent / "cover.webp").read_bytes() == WEBP


@pytest.mark.parametrize("native_track", ["aac", "webm", "mkv"], indirect=True)
def test_unsupported_native_formats_report_error(native_track: Path) -> None:
    original = native_track.read_bytes()
    assert tagger.main(["--file", str(native_track)]) == 1
    assert native_track.read_bytes() == original
