import copy
import logging
import sys
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

import pytest
from mediascan.types.track.track_yaml_file_validator import is_mapping, validate_track_yaml_content
from mutagen.id3 import ID3

from mediascripts.move import move_from_various_artists as move
from mediascripts.common import musicbrainz as catalogue_http

RECORDING = "0c652621-96d6-46bb-a886-b277b87bafd4"
GROUP = "30cd7bd2-ac63-4dd7-a745-864d298f34cf"
OTHER_GROUP = "ab25cd2f-ef7b-4569-8530-9d168def1020"
CREDIT: list[dict[str, object]] = [{"name": "Artist", "artist": {"name": "Artist"}}]


class _TagWriter(Protocol):
    def save(self, filething: Path) -> None: ...


@pytest.fixture
def source(tmp_path: Path) -> Path:
    album = tmp_path / "data" / "Various  Artists" / "Compilation [2005]"
    album.mkdir(parents=True)
    path = album / "01 - Track.yml"
    path.write_text(
        "# preserve track comment\n"
        "trackData:\n"
        "  artist: Artist\n"
        "  albumartist: Various Artists\n"
        "  title: Track\n"
        "  album: Compilation\n"
        "  genre: Rock\n"
        "  year: 2005\n"
        "mediaFileData:\n"
        "  - path: /data/Various  Artists/Compilation [2005]/01 - Track.mp3\n"
        "    size: 123\n"
    )
    (album / "album.yml").write_text("albumData: {name: Compilation, explicitCover: true}\n")
    (album / "cover.jpg").write_bytes(b"shared cover")
    return path


@pytest.fixture
def responses() -> dict[str, dict[str, object]]:
    group: dict[str, object] = {"id": GROUP, "primary-type": "Album", "secondary-types": []}
    release: dict[str, object] = {
        "status": "Official", "artist-credit": CREDIT, "release-group": group,
    }
    releases = [
        {**release, "release-group": {**group, "secondary-types": ["Compilation"]}},
        {**release, "status": "Bootleg"},
        release,
    ]
    return {
        "recording": {
            "count": 1,
            "recordings": [{"id": RECORDING, "title": "Track", "artist-credit": CREDIT, "releases": releases}],
        },
        f"release-group/{GROUP}": {
            **group, "artist-credit": CREDIT, "title": "Original Album", "first-release-date": "1995-04-03",
        },
    }


def set_embedded_releases(responses: dict[str, dict[str, object]], releases: list[dict[str, object]]) -> None:
    recordings = move.catalogue_items(responses["recording"], "recordings")
    recordings[0]["releases"] = releases


@pytest.fixture
def catalogue(
    monkeypatch: pytest.MonkeyPatch, responses: dict[str, dict[str, object]],
) -> MagicMock:
    client = MagicMock(spec=move.MusicBrainz)

    def get(endpoint: str, **_params: str) -> dict[str, object]:
        return copy.deepcopy(responses[endpoint])

    client.get.side_effect = get
    monkeypatch.setattr(move, "MusicBrainz", MagicMock(return_value=client))
    return client


def data_root(source: Path) -> Path:
    return source.parents[2]


def target(source: Path) -> Path:
    return data_root(source) / "Artist" / "Original Album [1995]" / source.name


def core(path: Path) -> dict[str, object]:
    data = validate_track_yaml_content(path.read_text())["trackData"]
    assert is_mapping(data)
    return data


@pytest.mark.parametrize("dry_run", [True, False])
def test_moves_one_yaml_track_to_original_album(
    source: Path, catalogue: MagicMock, dry_run: bool, capsys: pytest.CaptureFixture[str],
) -> None:
    root = data_root(source)
    original = source.read_bytes()
    sibling = source.with_name("02 - Track.yml")
    sibling.write_bytes(original)
    source_album = (source.parent / "album.yml").read_bytes()

    assert move.move_from_various_artists(root, dry_run=dry_run)

    catalogue.get.assert_any_call(
        "recording",
        query='artist:"Artist" AND recording:"Track" AND primarytype:album AND status:official'
        ' AND firstreleasedate:[* TO 2005-12-31]',
        limit="100",
    )
    assert sibling.read_bytes() == original
    assert (source.parent / "album.yml").read_bytes() == source_album
    assert (source.parent / "cover.jpg").read_bytes() == b"shared cover"
    output = capsys.readouterr().out
    assert "Original Album" in output and "1995" in output
    if dry_run:
        assert source.read_bytes() == original
        assert not (root / "Artist").exists()
    else:
        assert not source.exists()
        destination = target(source)
        assert core(destination) == {
            "artist": "Artist", "albumartist": "Artist", "title": "Track",
            "album": "Original Album", "genre": "Rock", "year": 1995,
        }
        content = destination.read_text()
        assert content.startswith("# preserve track comment\n")
        assert "/data/Artist/Original Album [1995]/01 - Track.mp3" in content
        assert "size: 123" in content
        album_data = move.yaml_document(destination.parent / "album.yml")["albumData"]
        assert album_data == {
            "name": "Original Album", "dateReleased": {"y": 1995}, "explicitCover": False,
        }


def test_moves_all_media_and_lyrics_and_reuses_case_variant_directories(
    source: Path, catalogue: MagicMock,
) -> None:
    media = ID3()
    move.text_values(media, "artist", "Artist")
    cast(_TagWriter, media).save(source.with_suffix(".mp3"))
    source.with_suffix(".txt").write_text("lyrics")
    source.with_suffix(".lrc").write_text("[00:01.00]lyrics")
    source.with_suffix(".flac").write_bytes(b"unreadable tags, but YAML and MP3 identify track")
    destination = data_root(source) / "aRTIST" / "original album [1995]"
    destination.mkdir(parents=True)
    album_yaml = destination / "ALBUM.YAML"
    original_album = (
        "# preserve album comment\nalbumData:\n  name: Original Album\n"
        "  dateReleased: {y: 1995, m: 4, d: 3}\n  explicitCover: true\n  genres: [Rock]\n"
    )
    album_yaml.write_text(original_album)

    assert move.move_from_various_artists(data_root(source))

    assert not (data_root(source) / "Artist").exists()
    assert not source.exists()
    for suffix in (".mp3", ".flac", ".txt", ".lrc", ".yml"):
        assert (destination / source.with_suffix(suffix).name).is_file()
        assert not source.with_suffix(suffix).exists()
    assert (destination / "01 - Track.txt").read_text() == "lyrics"
    assert album_yaml.read_text() == original_album
    assert not (destination / "album.yml").exists()


def test_generates_yaml_from_tags(source: Path, catalogue: MagicMock) -> None:
    source.unlink()
    media_path = source.with_suffix(".mp3")
    media = ID3()
    for field, value in {
        "artist": "Artist", "title": "Track", "album": "Compilation", "year": "2005", "genre": "Rock",
    }.items():
        move.text_values(media, field, value)
    cast(_TagWriter, media).save(media_path)
    before = media_path.read_bytes()
    assert move.move_from_various_artists(data_root(source))
    assert core(target(source))["album"] == "Original Album"
    assert target(source).with_suffix(".mp3").read_bytes() == before


def test_filename_artist_fallback(source: Path, catalogue: MagicMock) -> None:
    source.unlink()
    media_path = source.with_name("01 - Artist - Track.mp3")
    cast(_TagWriter, ID3()).save(media_path)
    assert move.move_from_various_artists(data_root(source))
    generated = target(source).with_name("01 - Artist - Track.yml")
    assert core(generated)["artist"] == "Artist"
    assert core(generated)["title"] == "Track"


def test_artist_prefixed_yaml_title_is_used_when_artist_is_various(source: Path, catalogue: MagicMock) -> None:
    source.write_text(
        "trackData: {artist: Various Artists, title: Artist - Track, genre: Rock, year: 2005}\n"
    )
    assert move.move_from_various_artists(data_root(source))
    assert core(target(source))["artist"] == "Artist"
    assert core(target(source))["title"] == "Track"


def test_empty_yaml_placeholders_are_populated(source: Path, catalogue: MagicMock) -> None:
    source.write_text("")
    media = ID3()
    move.text_values(media, "artist", "Artist")
    move.text_values(media, "title", "Track")
    cast(_TagWriter, media).save(source.with_suffix(".mp3"))
    destination = target(source).parent
    destination.mkdir(parents=True)
    (destination / "album.yml").touch()
    assert move.move_from_various_artists(data_root(source))
    assert core(target(source))["album"] == "Original Album"
    assert move.yaml_document(destination / "album.yml")["albumData"] == {
        "name": "Original Album", "dateReleased": {"y": 1995}, "explicitCover": False,
    }


@pytest.mark.parametrize("collision", ["01 - TRACK.yml", "01 - Track.MP3", "01 - Track.txt"])
def test_destination_collisions_leave_source_untouched(
    source: Path, catalogue: MagicMock, collision: str,
) -> None:
    destination = target(source).parent
    destination.mkdir(parents=True)
    existing = destination / collision
    existing.write_bytes(b"existing file")
    original = source.read_bytes()
    assert not move.move_from_various_artists(data_root(source))
    assert existing.read_bytes() == b"existing file"
    assert source.read_bytes() == original
    assert list(destination.iterdir()) == [existing]


def test_ambiguous_artist_directories_are_not_duplicated(source: Path, catalogue: MagicMock) -> None:
    root = data_root(source)
    (root / "Artist").mkdir()
    (root / "ARTIST").mkdir()
    assert not move.move_from_various_artists(root)
    assert source.exists()
    assert not (root / "Artist" / "Original Album [1995]").exists()


@pytest.mark.parametrize("broken", ["trackData: [broken]", "trackData: {artist: [invalid]}", "trackData: *undefined"])
def test_invalid_yaml_is_reported_without_changes(
    source: Path, catalogue: MagicMock, broken: str, caplog: pytest.LogCaptureFixture,
) -> None:
    source.write_text(broken)
    assert not move.move_from_various_artists(data_root(source))
    assert source.read_text() == broken
    assert "Skipping track" in caplog.text
    catalogue.get.assert_not_called()


def test_conflicting_embedded_artist_is_skipped(source: Path, catalogue: MagicMock) -> None:
    media = ID3()
    move.text_values(media, "artist", "Different Artist")
    cast(_TagWriter, media).save(source.with_suffix(".mp3"))
    assert not move.move_from_various_artists(data_root(source))
    catalogue.get.assert_not_called()
    assert source.exists()


@pytest.mark.parametrize("failure", ["copy", "track-yaml", "album-yaml", "source-unlink"])
def test_filesystem_failure_rolls_back(
    source: Path, catalogue: MagicMock, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    source.with_suffix(".txt").write_text("lyrics")
    original = source.read_bytes()
    real_write = move.write_yaml
    real_copy = move.copy_exclusively
    real_unlink = Path.unlink

    def write(path: Path, content: str, *, create_only: bool = False) -> None:
        if (failure == "track-yaml" and path.name == source.name) or (
            failure == "album-yaml" and path.name == "album.yml"
        ):
            raise OSError("injected write failure")
        real_write(path, content, create_only=create_only)

    def copy_file(src: Path, dst: Path) -> None:
        if failure == "copy":
            raise OSError("injected copy failure")
        real_copy(src, dst)

    def unlink(path: Path, missing_ok: bool = False) -> None:
        if failure == "source-unlink" and path == source:
            raise OSError("injected unlink failure")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(move, "write_yaml", write)
    monkeypatch.setattr(move, "copy_exclusively", copy_file)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError, match="injected"):
        move.move_from_various_artists(data_root(source))
    assert source.read_bytes() == original
    assert source.with_suffix(".txt").read_text() == "lyrics"
    assert not (data_root(source) / "Artist").exists()


def test_album_update_failure_restores_existing_album(
    source: Path, catalogue: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = target(source).parent
    destination.mkdir(parents=True)
    album_yaml = destination / "album.yml"
    original = "# incomplete album\nalbumData: {name: Original Album}\n"
    album_yaml.write_text(original)
    real_unlink = Path.unlink

    def unlink(path: Path, missing_ok: bool = False) -> None:
        if path == source:
            raise OSError("source is read-only")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError, match="read-only"):
        move.move_from_various_artists(data_root(source))
    assert album_yaml.read_text() == original
    assert list(destination.iterdir()) == [album_yaml]
    assert source.exists()


def test_network_failure_does_not_move(source: Path, catalogue: MagicMock) -> None:
    catalogue.get.side_effect = URLError("unavailable")
    with pytest.raises(URLError):
        move.move_from_various_artists(data_root(source))
    assert source.exists()
    assert not (data_root(source) / "Artist").exists()


def test_catalogue_rejects_compilations_and_live_albums(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]],
) -> None:
    set_embedded_releases(responses, [
            {"status": "Official", "artist-credit": CREDIT, "release-group": {
                "id": GROUP, "primary-type": "Album", "secondary-types": [kind],
            }}
            for kind in ("Compilation", "Live")
    ])
    assert not move.move_from_various_artists(data_root(source))
    assert source.exists()
    assert catalogue.get.call_count == 1


@pytest.mark.parametrize("mutation", ["wrong-title", "wrong-artist", "live", "video", "truncated-search"])
def test_uncertain_recording_is_not_moved(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]], mutation: str,
) -> None:
    recording: dict[str, object] = {"id": RECORDING, "title": "Track", "artist-credit": CREDIT}
    if mutation == "wrong-title":
        recording["title"] = "Track (Live)"
    elif mutation == "wrong-artist":
        recording["artist-credit"] = [{"name": "Another Artist"}]
    elif mutation == "live":
        recording["disambiguation"] = "live"
    elif mutation == "video":
        recording["video"] = True
    responses["recording"] = {"count": 101 if mutation == "truncated-search" else 1, "recordings": [recording]}
    assert not move.move_from_various_artists(data_root(source))
    assert source.exists()


def test_earliest_album_beats_later_reissue(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]],
) -> None:
    set_embedded_releases(responses, [
            {"status": "Official", "artist-credit": CREDIT, "release-group": {
                "id": identifier, "primary-type": "Album", "secondary-types": [],
            }} for identifier in (OTHER_GROUP, GROUP)
    ])
    responses[f"release-group/{OTHER_GROUP}"] = {
        "id": OTHER_GROUP, "primary-type": "Album", "artist-credit": CREDIT,
        "title": "Later Album", "first-release-date": "2000",
    }
    assert move.move_from_various_artists(data_root(source))
    assert core(target(source))["year"] == 1995


def test_ambiguous_same_year_albums_are_skipped(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]],
) -> None:
    set_embedded_releases(responses, [
            {"status": "Official", "artist-credit": CREDIT, "release-group": {
                "id": identifier, "primary-type": "Album", "secondary-types": [],
            }} for identifier in (GROUP, OTHER_GROUP)
    ])
    responses[f"release-group/{OTHER_GROUP}"] = {
        "id": OTHER_GROUP, "primary-type": "Album", "artist-credit": CREDIT,
        "title": "Another Album", "first-release-date": "1995",
    }
    assert not move.move_from_various_artists(data_root(source))
    assert source.exists()


def test_oldest_album_wins_across_recordings_even_when_source_year_matches_reissue(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]],
) -> None:
    responses["recording"] = {
        "count": 2, "recordings": [
            {"id": recording_id, "title": "Track", "artist-credit": CREDIT, "releases": [{
                "status": "Official", "artist-credit": CREDIT,
                "release-group": {"id": group_id, "primary-type": "Album", "secondary-types": []},
            }]}
            for recording_id, group_id in ((RECORDING, GROUP), (OTHER_GROUP, OTHER_GROUP))
        ],
    }
    responses[f"release-group/{OTHER_GROUP}"] = {
        "id": OTHER_GROUP, "primary-type": "Album", "artist-credit": CREDIT,
        "title": "Original Album (Deluxe Edition)", "first-release-date": "2005",
    }

    assert move.move_from_various_artists(data_root(source))
    assert core(target(source))["album"] == "Original Album"
    assert core(target(source))["year"] == 1995


@pytest.mark.parametrize("title", [
    "Original Album (Deluxe Edition)",
    "Original Album [2011 Remastered]",
    "Original Album - 25th Anniversary Edition",
    "Original Album (Super Deluxe Edition) (2011 Remaster)",
])
def test_original_title_is_used_in_directory_and_both_yaml_files(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]], title: str,
) -> None:
    responses[f"release-group/{GROUP}"]["title"] = title
    assert move.move_from_various_artists(data_root(source))
    assert core(target(source))["album"] == "Original Album"
    assert core(target(source))["year"] == 1995
    assert move.yaml_document(target(source).parent / "album.yml")["albumData"] == {
        "name": "Original Album", "dateReleased": {"y": 1995}, "explicitCover": False,
    }


def test_existing_album_metadata_is_corrected_to_original(
    source: Path, catalogue: MagicMock,
) -> None:
    destination = target(source).parent
    destination.mkdir(parents=True)
    album_yaml = destination / "album.yml"
    album_yaml.write_text(
        "# preserve album comment\nalbumData:\n  name: Original Album (Deluxe Edition)\n"
        "  dateReleased: {y: 2005, m: 6, d: 7}\n  explicitCover: true\n  genres: [Rock]\n"
    )
    assert move.move_from_various_artists(data_root(source))
    assert move.yaml_document(album_yaml)["albumData"] == {
        "name": "Original Album", "dateReleased": {"y": 1995}, "explicitCover": True, "genres": ["Rock"],
    }
    assert album_yaml.read_text().startswith("# preserve album comment\n")


@pytest.mark.parametrize(("title", "expected"), [
    ("Album (Deluxe)", "Album"),
    ("Album [Expanded Edition]", "Album"),
    ("Album (Collector's Edition)", "Album"),
    ("Album (Remastered 2011)", "Album"),
    ("Album (20th Anniversary Edition)", "Album"),
    ("Album (Deluxe Edition, Remastered)", "Album"),
    ("Album - Part II - Deluxe Edition", "Album - Part II"),
    ("Album (Part II) (Deluxe Edition)", "Album (Part II)"),
    ("(What's the Story) Morning Glory?", "(What's the Story) Morning Glory?"),
    ("Album (Part II)", "Album (Part II)"),
    ("Album (Deluxe Life)", "Album (Deluxe Life)"),
    ("Deluxe", "Deluxe"),
    ("(Deluxe Edition)", "(Deluxe Edition)"),
    ("Album - The Original", "Album - The Original"),
])
def test_original_album_title_only_removes_edition_labels(title: str, expected: str) -> None:
    assert move.original_album_title(title) == expected


def test_edition_groups_with_same_original_title_are_not_ambiguous(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]],
) -> None:
    set_embedded_releases(responses, [
            {"status": "Official", "artist-credit": CREDIT, "release-group": {
                "id": identifier, "primary-type": "Album", "secondary-types": [],
            }} for identifier in (GROUP, OTHER_GROUP)
    ])
    responses[f"release-group/{OTHER_GROUP}"] = {
        "id": OTHER_GROUP, "primary-type": "Album", "artist-credit": CREDIT,
        "title": "Original Album (Deluxe Edition)", "first-release-date": "1995",
    }
    assert move.move_from_various_artists(data_root(source))
    assert core(target(source))["album"] == "Original Album"
    assert core(target(source))["year"] == 1995


def test_dry_run_does_not_change_existing_edition_metadata(
    source: Path, catalogue: MagicMock, capsys: pytest.CaptureFixture[str],
) -> None:
    destination = target(source).parent
    destination.mkdir(parents=True)
    album_yaml = destination / "album.yml"
    original = "albumData: {name: Original Album (Deluxe Edition), dateReleased: {y: 2005}}\n"
    album_yaml.write_text(original)
    assert move.move_from_various_artists(data_root(source), dry_run=True)
    assert source.exists()
    assert not target(source).exists()
    assert album_yaml.read_text() == original
    output = capsys.readouterr().out
    assert "+  name: Original Album" in output or "name: Original Album," in output
    assert "1995" in output


def test_unrelated_destination_album_name_is_not_overwritten(source: Path, catalogue: MagicMock) -> None:
    destination = target(source).parent
    destination.mkdir(parents=True)
    album_yaml = destination / "album.yml"
    original = "albumData: {name: Unrelated Album, dateReleased: {y: 2005}}\n"
    album_yaml.write_text(original)
    assert not move.move_from_various_artists(data_root(source))
    assert source.exists()
    assert album_yaml.read_text() == original


def test_eighty_one_recordings_use_one_search_and_one_group_lookup(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]],
) -> None:
    release: dict[str, object] = {
        "status": "Official", "artist-credit": CREDIT,
        "release-group": {"id": GROUP, "primary-type": "Album", "secondary-types": []},
    }

    responses["recording"] = {
        "count": 81, "recordings": [
            {"id": f"{index:08x}-96d6-46bb-a886-b277b87bafd4", "title": "Track",
             "artist-credit": CREDIT, "releases": [release]}
            for index in range(81)
        ],
    }
    assert move.move_from_various_artists(data_root(source))
    assert catalogue.get.call_count == 2
    assert all(call.args[0] != "release" for call in catalogue.get.call_args_list)
    assert len([
        call for call in catalogue.get.call_args_list if call.args[0] == f"release-group/{GROUP}"
    ]) == 1


def test_embedded_search_releases_can_omit_artist_credit(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]],
) -> None:
    set_embedded_releases(responses, [{
        "id": OTHER_GROUP, "title": "Original Album (Deluxe Edition)",
        "status": "Official", "date": "2005",
        "release-group": {"id": GROUP, "title": "Original Album", "primary-type": "Album"},
    }])
    recording = move.catalogue_items(responses["recording"], "recordings")[0]
    recording["first-release-date"] = "1993"  # Earlier single, not the album release.
    assert move.move_from_various_artists(data_root(source))
    assert core(target(source))["year"] == 1995
    assert core(target(source))["album"] == "Original Album"
    assert catalogue.get.call_count == 2
    query = catalogue.get.call_args_list[0].kwargs["query"]
    assert "primarytype:album AND status:official" in query
    assert "firstreleasedate:[* TO 2005-12-31]" in query
    assert "-secondarytype:*" not in query
    assert "firstreleasedate:2005*" not in query


def test_complete_embedded_original_album_metadata_needs_only_one_request(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]],
) -> None:
    set_embedded_releases(responses, [{
        "status": "Official", "release-group": responses[f"release-group/{GROUP}"],
    }])
    assert move.move_from_various_artists(data_root(source))
    assert core(target(source))["album"] == "Original Album"
    assert core(target(source))["year"] == 1995
    assert catalogue.get.call_count == 1


def test_album_group_credit_is_still_verified_without_release_credit(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]],
) -> None:
    set_embedded_releases(responses, [{
        "status": "Official",
        "release-group": {"id": GROUP, "primary-type": "Album"},
    }])
    responses[f"release-group/{GROUP}"]["artist-credit"] = [{"name": "Another Artist"}]
    assert not move.move_from_various_artists(data_root(source))
    assert source.exists()
    assert catalogue.get.call_count == 2


def test_missing_embedded_releases_are_not_used(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]],
) -> None:
    del move.catalogue_items(responses["recording"], "recordings")[0]["releases"]
    assert not move.move_from_various_artists(data_root(source))
    assert source.exists()


def test_unidentifiable_track_does_not_block_next_track(source: Path, catalogue: MagicMock) -> None:
    unknown = source.with_name("00 - Unknown.yml")
    unknown.write_text("trackData: {artist: Various Artists, title: Unknown}\n")
    assert move.move_from_various_artists(data_root(source))
    assert unknown.exists()
    assert not source.exists()
    assert target(source).exists()


def test_nested_library_keeps_destination_artists_in_same_library(source: Path, catalogue: MagicMock) -> None:
    root = data_root(source)
    library = root / "Music"
    library.mkdir()
    various = source.parent.parent
    various.rename(library / various.name)
    nested_source = library / various.name / source.parent.name / source.name
    nested_source.write_text(nested_source.read_text().replace("/data/", "/data/Music/"))
    assert move.move_from_various_artists(root)
    destination = library / "Artist" / "Original Album [1995]" / source.name
    assert destination.exists()
    assert "/data/Music/Artist/Original Album [1995]/01 - Track.mp3" in destination.read_text()
    assert not (root / "Artist").exists()


def test_duplicate_yaml_sidecars_are_skipped(source: Path, catalogue: MagicMock) -> None:
    source.with_suffix(".yaml").write_bytes(source.read_bytes())
    assert not move.move_from_various_artists(data_root(source))
    assert source.exists()
    catalogue.get.assert_not_called()


def test_copy_metadata_failure_removes_partial_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("content")

    def fail_stat(_src: Path, _dst: Path) -> None:
        raise OSError("metadata copy failed")

    monkeypatch.setattr(move.shutil, "copystat", fail_stat)
    with pytest.raises(OSError, match="metadata copy failed"):
        move.copy_exclusively(source, destination)
    assert source.read_text() == "content"
    assert not destination.exists()


def test_cli_reports_network_failure(
    source: Path, catalogue: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogue.get.side_effect = URLError("unavailable")
    monkeypatch.setattr(sys, "argv", ["move-from-various-artists", "--dir", str(data_root(source))])
    assert move.main() == 1
    assert source.exists()


def test_interrupt_after_source_deletion_restores_original_yaml(
    source: Path, catalogue: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = source.read_bytes()
    real_unlink = Path.unlink

    def unlink(path: Path, missing_ok: bool = False) -> None:
        real_unlink(path, missing_ok=missing_ok)
        if path == source:
            raise KeyboardInterrupt

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(KeyboardInterrupt):
        move.move_from_various_artists(data_root(source))
    assert source.read_bytes() == original
    assert not target(source).exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_default_and_dry_run(
    source: Path, catalogue: MagicMock, monkeypatch: pytest.MonkeyPatch, dry_run: bool,
) -> None:
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(data_root(source).parent))
    monkeypatch.setattr(sys, "argv", ["move-from-various-artists", *(["-n"] if dry_run else [])])
    assert move.main() == 0
    assert source.exists() == dry_run
    assert target(source).exists() != dry_run


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("count", [0, 1, 2, 5, None])
def test_count_limits_successful_tracks(
    source: Path, catalogue: MagicMock, dry_run: bool, count: int | None,
) -> None:
    original = source.read_bytes()
    siblings = [source.with_name(f"0{index} - Track.yml") for index in (2, 3)]
    for sibling in siblings:
        sibling.write_bytes(original)
    unknown = source.with_name("00 - Unknown.yml")
    unknown.write_text("trackData: {artist: Various Artists, title: Unknown}\n")
    expected = min(count, 3) if count is not None else 3

    assert move.move_from_various_artists(data_root(source), count=count, dry_run=dry_run) == (expected > 0)

    assert catalogue.get.call_count == expected * 2
    assert unknown.exists()
    tracks = [source, *siblings]
    if dry_run:
        assert all(path.read_bytes() == original for path in tracks)
        assert not target(source).parent.exists()
    else:
        assert sum(not path.exists() for path in tracks) == expected
        assert sum((target(source).parent / path.name).exists() for path in tracks) == expected


@pytest.mark.parametrize("flag", ["-c", "--count"])
def test_cli_forwards_count_and_sleep(
    source: Path, monkeypatch: pytest.MonkeyPatch, flag: str,
) -> None:
    runner = MagicMock(return_value=True)
    monkeypatch.setattr(move, "move_from_various_artists", runner)
    monkeypatch.setattr(sys, "argv", [
        "move-from-various-artists", "--dir", str(data_root(source)),
        flag, "3", "--sleep", "2.5", "-n",
    ])
    assert move.main() == 0
    runner.assert_called_once_with(data_root(source), count=3, sleep=2.5, dry_run=True)


@pytest.mark.parametrize("arguments", [
    ["--count", "-1"], ["--sleep", "0"], ["--sleep", "-1"],
    ["--sleep", "nan"], ["--sleep", "inf"], ["--sleep", "not-a-number"],
])
def test_cli_rejects_invalid_limits(monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["move-from-various-artists", *arguments])
    with pytest.raises(SystemExit) as error:
        move.main()
    assert error.value.code == 2


@pytest.mark.parametrize("sleep", [0, -1, float("nan"), float("inf")])
def test_api_rejects_invalid_sleep(source: Path, sleep: float) -> None:
    with pytest.raises(ValueError, match="sleep must"):
        move.move_from_various_artists(data_root(source), sleep=sleep)
    with pytest.raises(ValueError, match="sleep must"):
        catalogue_http.MusicBrainz(sleep=sleep)


def test_api_rejects_negative_count(source: Path) -> None:
    with pytest.raises(ValueError, match="count must"):
        move.move_from_various_artists(data_root(source), count=-1)


def test_zero_count_does_not_scan_or_request(tmp_path: Path, catalogue: MagicMock) -> None:
    assert not move.move_from_various_artists(tmp_path / "not-present", count=0)
    catalogue.get.assert_not_called()


def test_sleep_is_forwarded_to_catalogue(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    factory = MagicMock()
    factory.return_value.get.return_value = {"count": 0, "recordings": []}
    monkeypatch.setattr(move, "MusicBrainz", factory)
    assert not move.move_from_various_artists(data_root(source), sleep=3.5)
    factory.assert_called_once_with(sleep=3.5)


@pytest.mark.parametrize("retry", [False, True])
def test_catalogue_honors_sleep_interval(monkeypatch: pytest.MonkeyPatch, retry: bool) -> None:
    responses: list[object] = (
        [TimeoutError("temporary"), BytesIO(b"{}")] if retry
        else [BytesIO(b"{}"), BytesIO(b"{}")]
    )
    network = MagicMock(side_effect=responses)
    sleeper = MagicMock()
    monkeypatch.setattr(catalogue_http, "urlopen", network)
    monkeypatch.setattr(catalogue_http.time, "sleep", sleeper)
    monkeypatch.setattr(catalogue_http.time, "monotonic", MagicMock(side_effect=[10.0, 10.25, 13.5]))
    client = catalogue_http.MusicBrainz(sleep=3.5)
    assert client.get("recording", query="test") == {}
    if not retry:
        assert client.get("release", query="test") == {}
    sleeper.assert_any_call(3.25)
    assert network.call_count == 2


def test_logging_explains_recording_and_release_selection(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses["recording"] = {
        "count": 2,
        "recordings": [
            {"id": RECORDING, "title": "Track", "artist-credit": CREDIT},
            {"id": OTHER_GROUP, "title": "Track", "artist-credit": CREDIT, "disambiguation": "live"},
        ],
    }
    set_embedded_releases(responses, [
        {"status": "Official", "artist-credit": CREDIT, "release-group": {
            "id": GROUP, "primary-type": "Album", "secondary-types": ["Compilation"],
        }},
        {"status": "Bootleg", "artist-credit": CREDIT},
        {"status": "Official", "artist-credit": CREDIT, "release-group": {
            "id": GROUP, "primary-type": "Album", "secondary-types": [],
        }},
    ])
    with caplog.at_level(logging.DEBUG):
        assert move.move_from_various_artists(data_root(source), dry_run=True, count=1, sleep=10)
    for expected in (
        "dry_run=True count=1 sleep=10.00s",
        "count limits successful tracks, not MusicBrainz requests",
        "dry-run still performs online lookups",
        "Starting track 1",
        "returned 2 recording(s), total=2",
        "source album='Compilation' year hint=2005",
        "Examining recording 1/2",
        "filtering embedded releases locally (no request for this recording)",
        "3 embedded release(s)",
        "Eligible studio album: 'Original Album' [1995]",
        "3 release(s), 1 eligible studio album group(s)",
        "'secondary release type (e.g. compilation/live/remix)': 1",
        "'not Official': 1",
        "disambiguated version",
        "1 examined locally, 1 rejected",
        "Track limit reached; stopping (attempted=1 completed=1 skipped=0)",
    ):
        assert expected in caplog.text
    assert source.exists()


def test_logging_explains_no_eligible_albums(
    source: Path, catalogue: MagicMock, responses: dict[str, dict[str, object]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    set_embedded_releases(responses, [])
    with caplog.at_level(logging.INFO):
        assert not move.move_from_various_artists(data_root(source), dry_run=True)
    assert "0 release(s), 0 eligible studio album group(s)" in caplog.text
    assert "Unresolved album candidates: []" in caplog.text
    assert "attempted=1 completed=0 skipped=1 dry_run=True" in caplog.text


def test_network_failure_logs_track_context_and_progress(
    source: Path, catalogue: MagicMock, caplog: pytest.LogCaptureFixture,
) -> None:
    catalogue.get.side_effect = URLError("unavailable")
    with caplog.at_level(logging.INFO), pytest.raises(URLError):
        move.move_from_various_artists(data_root(source), dry_run=True)
    assert f"[{source}] Track processing failed after" in caplog.text
    assert "stopping run (completed=0 skipped=0 dry_run=True)" in caplog.text
    assert source.exists()


def test_retry_logging_distinguishes_backoff_from_ten_second_pacing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    clock = 0.0
    starts: list[float] = []
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal clock
        waits.append(seconds)
        clock += seconds

    def fail_request(*_args: object, **_kwargs: object) -> None:
        starts.append(clock)
        headers = Message()
        headers["Retry-After"] = "1"
        raise HTTPError("https://musicbrainz.org/ws/2/release", 503, "Unavailable", headers, BytesIO(b""))

    monkeypatch.setattr(catalogue_http.time, "monotonic", lambda: clock)
    monkeypatch.setattr(catalogue_http.time, "sleep", sleep)
    monkeypatch.setattr(catalogue_http, "urlopen", fail_request)
    with caplog.at_level(logging.INFO), pytest.raises(HTTPError):
        catalogue_http.MusicBrainz(sleep=10).get("release", recording=RECORDING)

    assert starts == [0, 10, 20, 30]
    assert waits == [1, 9, 1, 9, 1, 9]
    assert "Retry-After header: '1'; retry backoff: 1.00s" in caplog.text
    assert "1.00s backoff; the 10.00s minimum request-start interval is enforced separately" in caplog.text
    assert "rate-limit wait: 9.00s (minimum request-start interval: 10.00s; attempt 2/4)" in caplog.text
    assert "retry limit exhausted" in caplog.text


def test_verbose_cli_enables_debug_with_timestamps(source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configure = MagicMock()
    monkeypatch.setattr(move.logging, "basicConfig", configure)
    monkeypatch.setattr(move, "move_from_various_artists", MagicMock(return_value=True))
    monkeypatch.setattr(sys, "argv", ["move-from-various-artists", "-v", "--dir", str(data_root(source))])
    assert move.main() == 0
    configure.assert_called_once_with(
        level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    )
