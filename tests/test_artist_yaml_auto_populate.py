import logging
from pathlib import Path

import pytest
import yaml
from mediascan.types.artist.artist_yaml_file import ArtistYamlFile
from mutagen.id3 import ID3, TPE1, TPE2

from mediascripts.artist.artist_yaml import artist_yaml_auto_populate as populate


@pytest.fixture
def artist(tmp_path: Path) -> Path:
    directory = tmp_path / "Artist_Name"
    (directory / "Album [2001]").mkdir(parents=True)
    return directory


def track_yaml(artist: Path, stem: str, data: object) -> Path:
    path = artist / "Album [2001]" / f"{stem}.yml"
    path.write_text(yaml.safe_dump({"trackData": data}), encoding="utf-8")
    return path


def media_file(artist: Path, name: str, albumartist: str = "", performer: str = "") -> Path:
    path = artist / "Album [2001]" / f"{name}.mp3"
    tags = ID3()
    if albumartist:
        tags.add(TPE2(encoding=3, text=[albumartist]))
    if performer:
        tags.add(TPE1(encoding=3, text=[performer]))
    tags.save(path)
    return path


def test_yaml_preferred_over_media_and_album_artist_over_performer(artist: Path) -> None:
    track_yaml(artist, "01", {"artist": "Guest", "albumartist": "YAML Artist"})
    media_file(artist, "01", "Tagged Artist")
    path, created = populate.ensure_artist_yaml(artist, {})
    assert created
    data = yaml.safe_load(path.read_text())["artistData"]
    assert data == {
        "artistNames": ["YAML Artist"], "city": "", "countryCode": "",
        "regionCode": "", "languageCodes": [],
    }
    loaded = ArtistYamlFile.from_yaml(path.read_text())
    assert loaded.artist_data.artist_names == ["YAML Artist"]


def test_cli_names_take_precedence_without_scanning(
    artist: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_scan(*args: object) -> str:
        pytest.fail("Explicit artist names should not require local scanning")

    monkeypatch.setattr(populate, "infer_artist_name", fail_scan)
    assert populate.main([
        "--dir", str(artist), "--artist", "CLI Artist", "--artist", "Alias",
        "--city", "London", "--country-code", "GB", "--region-code", "GB-LND",
        "--language-code", "en", "--language-code", "fr", "--llm=false",
    ]) == 0
    assert yaml.safe_load((artist / "artist.yml").read_text())["artistData"] == {
        "artistNames": ["CLI Artist", "Alias"], "city": "London", "countryCode": "GB",
        "regionCode": "GB-LND", "languageCodes": ["en", "fr"],
    }


@pytest.mark.parametrize("albumartist,performer,expected", [
    ("Album Artist", "Guest", "Album Artist"), ("", "Performer", "Performer"),
])
def test_media_fallback(artist: Path, albumartist: str, performer: str, expected: str) -> None:
    track_yaml(artist, "01", {"title": "No artist"})
    media = media_file(artist, "01", albumartist, performer)
    before = media.read_bytes()
    assert populate.infer_artist_name(artist) == expected
    assert media.read_bytes() == before


def test_majority_vote_is_deterministic(artist: Path, caplog: pytest.LogCaptureFixture) -> None:
    for stem, name in [("01", "Guest"), ("02", "Main"), ("03", "Main")]:
        track_yaml(artist, stem, {"artist": name})
    assert populate.infer_artist_name(artist) == "Main"
    assert "Conflicting YAML artist names" in caplog.text


def test_malformed_clues_warn_and_fall_back(artist: Path, caplog: pytest.LogCaptureFixture) -> None:
    (artist / "Album [2001]" / "01.yml").write_text("trackData: [")
    media_file(artist, "01", "Tagged Artist")
    assert populate.infer_artist_name(artist) == "Tagged Artist"
    assert "Could not read artist clues" in caplog.text


def test_directory_fallback_and_ignore_nested_or_album_metadata(artist: Path) -> None:
    track_yaml(artist, "album", {"artist": "Not a track"})
    nested = artist / "Album [2001]" / "nested"
    nested.mkdir()
    (nested / "01.yml").write_text("trackData:\n  artist: Nested\n")
    (artist / "root.yml").write_text("trackData:\n  artist: Root\n")
    assert populate.infer_artist_name(artist) == "Artist.Name"


def test_existing_file_is_not_overwritten(artist: Path) -> None:
    path = artist / "artist.yml"
    path.write_text("# existing\nartistData:\n  artistNames: [Original]\n")
    before = path.read_bytes()
    assert populate.ensure_artist_yaml(artist, {"artistNames": ["CLI"]}) == (path, False)
    assert path.read_bytes() == before


def test_creation_never_replaces_a_concurrently_created_file(
    artist: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = artist / "artist.yml"

    def competing_link(source: Path, destination: Path) -> None:
        destination.write_text("created by another process\n")
        raise FileExistsError("destination exists")

    monkeypatch.setattr(populate.os, "link", competing_link)
    with pytest.raises(FileExistsError):
        populate.ensure_artist_yaml(artist, {"artistNames": ["New"]})
    assert path.read_text() == "created by another process\n"
    assert not list(artist.glob(".artist.yml.*.tmp"))


def test_creation_failure_is_reported_without_partial_yaml(
    artist: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def denied_link(source: Path, destination: Path) -> None:
        raise PermissionError("permission denied")

    monkeypatch.setattr(populate.os, "link", denied_link)
    assert populate.main(["--dir", str(artist), "--llm=false"]) == 1
    assert "permission denied" in caplog.text
    assert not (artist / "artist.yml").exists()
    assert not list(artist.glob(".artist.yml.*.tmp"))


@pytest.mark.parametrize("existing", [False, True])
def test_dir_continues_into_enrichment(
    artist: Path, existing: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = artist / "artist.yml"
    if existing:
        path.write_text("artistData:\n  artistNames: [Existing]\n")
    calls: list[dict[str, object]] = []

    def main_loop(**kwargs: object) -> None:
        assert path.is_file()
        calls.append(kwargs)

    monkeypatch.setattr(populate, "main_loop", main_loop)
    assert populate.main(["--dir", str(artist), "--sleep", "0"]) == 0
    assert calls == [{
        "sleep_between_files": 0, "llm_enabled": True, "specific_file": path, "force": not existing,
        "reference_artist_dir_pattern": "A*",
        "quiet": False,
    }]


def test_enrichment_receives_created_yaml_outside_collection(
    artist: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(populate, "MOONGAS_COLLECTION_ROOTDIR", str(tmp_path / "missing_collection"))
    monkeypatch.setattr(populate, "get_client", lambda: None)
    processed: list[Path] = []

    def process(input_path: Path, *args: object, **kwargs: object) -> bool:
        assert yaml.safe_load(input_path.read_text())["artistData"]["artistNames"] == ["Artist.Name"]
        processed.append(input_path)
        return True

    monkeypatch.setattr(populate, "process_artist_yaml_file", process)
    assert populate.main(["--dir", str(artist), "--sleep", "0"]) == 0
    assert processed == [artist / "artist.yml"]


def test_llm_disabled_creates_locally_without_client(
    artist: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_client() -> None:
        pytest.fail("Local-only mode must not create an LLM client")

    monkeypatch.setattr(populate, "get_client", fail_client)
    assert populate.main(["--dir", str(artist), "--llm=false"]) == 0
    assert (artist / "artist.yml").is_file()


@pytest.mark.parametrize("source,expected", [
    ("yaml", "Track Artist"), ("media", "Tagged Artist"), ("directory", "Band.Name"),
])
def test_collection_creates_and_enriches_missing_artist_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, expected: str,
) -> None:
    artist = tmp_path / "data" / "Music" / "Band_Name"
    (artist / "Album [2001]").mkdir(parents=True)
    if source == "yaml":
        track_yaml(artist, "01", {"albumartist": expected})
    elif source == "media":
        media_file(artist, "01", albumartist=expected)
    monkeypatch.setattr(populate, "MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    monkeypatch.setattr(populate, "get_client", lambda: None)
    processed: list[Path] = []

    def process(input_path: Path, *args: object, **kwargs: object) -> bool:
        assert yaml.safe_load(input_path.read_text())["artistData"]["artistNames"] == [expected]
        processed.append(input_path.resolve())
        return True

    monkeypatch.setattr(populate, "process_artist_yaml_file", process)
    populate.main_loop(0)

    assert processed == [(artist / "artist.yml").resolve()]
    assert not (artist / "Album [2001]" / "artist.yml").exists()


def test_collection_discovery_preserves_existing_files_and_creates_all_missing(
    tmp_path: Path,
) -> None:
    for name in ("Artist", "Band", "Cat"):
        (tmp_path / name / "Album [2001]").mkdir(parents=True)
    existing = tmp_path / "Band" / "artist.yml"
    existing.write_text("# existing artist\n")
    (tmp_path / "Container").mkdir()

    created = populate.create_missing_artist_yaml(tmp_path)

    assert created == {
        (tmp_path / "Cat" / "artist.yml").resolve(),
        (tmp_path / "Artist" / "artist.yml").resolve(),
    }
    assert existing.read_text() == "# existing artist\n"
    assert (tmp_path / "Artist" / "artist.yml").is_file()
    assert not (tmp_path / "Container" / "artist.yml").exists()


def test_collection_llm_disabled_does_not_create_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    artist = tmp_path / "data" / "Music" / "Band"
    (artist / "Album [2001]").mkdir(parents=True)
    monkeypatch.setattr(populate, "MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))

    populate.main_loop(0, llm_enabled=False)

    assert not (artist / "artist.yml").exists()


@pytest.mark.parametrize("pattern,reference,missing,other", [
    ("A*", "Alpha", "Another", "123 Band"),
    ("Ref?*", "RefOne", "RefTwo", "!Band"),
    ("*", "Existing", "Missing", None),
    ("", "Existing", "Missing", "Other"),
])
def test_reference_pattern_selects_examples_but_processes_missing_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    pattern: str, reference: str, missing: str, other: str | None,
) -> None:
    data = tmp_path / "data" / "Music"
    names = [reference, missing] + ([other] if other is not None else [])
    for name in names:
        (data / name / "Album [2001]").mkdir(parents=True)
    existing = data / reference / "artist.yml"
    existing.write_text("artistData:\n  artistNames: [Reference]\n")
    monkeypatch.setattr(populate, "MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    monkeypatch.setattr(populate, "get_client", lambda: None)
    monkeypatch.setattr(populate, "try_load_artist_yaml_for_skip", lambda path: False)
    processed: list[Path] = []
    examples: list[str] = []

    def process(input_path: Path, reference_examples: str, *args: object, **kwargs: object) -> bool:
        processed.append(input_path.resolve())
        examples.append(reference_examples)
        return True

    monkeypatch.setattr(populate, "process_artist_yaml_file", process)
    populate.main_loop(0, reference_artist_dir_pattern=pattern)

    expected = {(data / missing / "artist.yml").resolve()}
    if other is not None:
        expected.add((data / other / "artist.yml").resolve())
    if pattern == "":
        expected.add(existing.resolve())
    assert set(processed) == expected
    assert len(processed) == len(expected)
    assert all(("Reference" in example) == (pattern != "") for example in examples)
    assert all(f"{missing}/artist.yml" not in example for example in examples)


def test_cli_reference_pattern_is_forwarded(
    artist: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(populate, "main_loop", lambda **kwargs: calls.append(kwargs))

    assert populate.main([
        "--dir", str(artist), "--reference-artist-dir-pattern", "[AB]*",
    ]) == 0

    assert calls[0]["reference_artist_dir_pattern"] == "[AB]*"


@pytest.mark.parametrize("flag", ["-q", "--quiet"])
def test_cli_quiet_is_forwarded(
    artist: Path, monkeypatch: pytest.MonkeyPatch, flag: str,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(populate, "main_loop", lambda **kwargs: calls.append(kwargs))

    assert populate.main(["--dir", str(artist), flag]) == 0

    assert calls[0]["quiet"] is True


@pytest.mark.parametrize("quiet", [False, True])
def test_quiet_suppresses_valid_skip_notices_without_changing_skip_behavior(
    artist: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, quiet: bool,
) -> None:
    path = artist / "artist.yml"
    path.write_text("artistData:\n  artistNames: [Artist]\n")
    original = path.read_bytes()
    monkeypatch.setattr(populate, "get_client", lambda: None)
    monkeypatch.setattr(populate, "try_load_artist_yaml_for_skip", lambda path: True)
    monkeypatch.setattr(populate, "validate_artist_yaml_file", lambda *args: None)
    processed: set[Path] = set()

    with caplog.at_level(logging.INFO):
        assert populate.process_artist_yaml_files(
            artist, "A*", processed, specific_file=path, quiet=quiet,
        ) is None

    assert ("Skipping already-valid artist YAML file" in caplog.text) is not quiet
    assert ("All matching files are already valid" in caplog.text) is not quiet
    assert processed == {path}
    assert path.read_bytes() == original


def test_quiet_preserves_error_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(populate, "get_client", lambda: None)
    assert populate.process_artist_yaml_files(
        tmp_path, "A*", set(), specific_file=tmp_path / "missing.yml", quiet=True,
    ) is False
    assert "Specified file does not exist" in caplog.text


def test_collection_creation_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "data" / "Music" / "Band" / "Album [2001]").mkdir(parents=True)
    monkeypatch.setattr(populate, "MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))

    def denied(artist_dir: Path, overrides: object) -> tuple[Path, bool]:
        raise PermissionError("cannot create seed")

    monkeypatch.setattr(populate, "ensure_artist_yaml", denied)

    assert populate.main(["--sleep", "0"]) == 1
    assert "cannot create seed" in caplog.text


def test_invalid_directory_fails(tmp_path: Path) -> None:
    assert populate.main(["--dir", str(tmp_path / "missing"), "--llm=false"]) == 1


@pytest.mark.parametrize("args", [
    ["--dir", "artist", "--file", "artist.yml"], ["--artist", "Name"],
    ["--dir", "artist", "--artist", ""], ["--dir", "artist", "--clean"],
])
def test_invalid_argument_combinations(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        populate.main(args)
    assert error.value.code == 2
