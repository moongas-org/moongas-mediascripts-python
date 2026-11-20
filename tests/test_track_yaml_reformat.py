import logging
import os
import shlex
import sys
from pathlib import Path

import pytest
from ruamel.yaml.nodes import MappingNode, Node, SequenceNode

from mediascripts.common.yaml import create_yaml
from mediascripts.track.track_yaml.track_yaml_reformat import main, reformat_track_yaml


def assert_block_collections(node: Node, seen: set[int] | None = None) -> None:
    seen = set() if seen is None else seen
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, SequenceNode):
        if node.value:
            assert not node.flow_style
        for child in node.value:
            assert_block_collections(child, seen)
    elif isinstance(node, MappingNode):
        if node.value:
            assert not node.flow_style
        for key, value in node.value:
            assert_block_collections(key, seen)
            assert_block_collections(value, seen)


def test_reformat_preserves_content_comments_quotes_aliases_and_mode(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "song.yml"
    original = """\
# Header
trackData: {title: "Song", artists: ['One', 'Two'], nested: {empty: [], tags: {}}} # track
shared: &shared [one, {name: "two"}]
alias: *shared
"""
    path.write_text(original, encoding="utf-8")
    path.chmod(0o640)

    with caplog.at_level(logging.INFO):
        assert reformat_track_yaml(path)
    backup_path = path.with_name(f"{path.name}.bak")
    assert backup_path.read_text(encoding="utf-8") == original
    assert [message.strip() for message in caplog.messages] == [
        shlex.join(["meld", str(backup_path.resolve()), str(path.resolve())])
    ]
    result = path.read_text(encoding="utf-8")
    assert "# Header" in result and "# track" in result
    assert '"Song"' in result and "'One'" in result
    assert "empty: []" in result and "tags: {}" in result
    yaml = create_yaml()
    parsed = yaml.load(result)
    assert parsed["shared"] is parsed["alias"]
    assert_block_collections(yaml.compose(result))
    assert path.stat().st_mode & 0o777 == 0o640
    assert not reformat_track_yaml(path)
    assert path.read_text(encoding="utf-8") == result


def test_block_and_empty_only_files_are_not_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "song.yml"
    original = "trackData:\n  title: Song\n  tags: []\n  ids: {}\n"
    path.write_text(original, encoding="utf-8")
    inode = path.stat().st_ino

    assert not reformat_track_yaml(path)
    assert path.stat().st_ino == inode
    assert path.read_text(encoding="utf-8") == original


def test_unchanged_files_do_not_require_a_second_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "song.yml"
    path.write_text(
        "trackData:\n  title: Song\nmediaFileData:\n"
        "  - acoustidData:\n      results:\n        - recordings: []\n",
        encoding="utf-8",
    )

    def unexpected_compose(*args: object, **kwargs: object) -> None:
        pytest.fail("Unchanged YAML should not be parsed twice")

    monkeypatch.setattr(type(create_yaml()), "compose", unexpected_compose)

    assert not reformat_track_yaml(path)
    assert not path.with_suffix(".yml.bak").exists()


def test_cli_reports_scan_and_progress_even_when_nothing_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    paths = [tmp_path / f"{index:02}.yml" for index in range(4)]
    for path in paths:
        path.write_text("trackData:\n  title: Song\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["track-yaml-reformat", "--dir", str(tmp_path), "--dry-run"])
    ticks = iter([0.0, 1.0, 5.0, 6.0])
    monkeypatch.setattr(
        "mediascripts.track.track_yaml.track_yaml_reformat.time.monotonic", lambda: next(ticks),
    )

    def unchanged(path: Path, dry_run: bool = False) -> bool:
        assert dry_run
        assert f"Scanning for track YAML files in {tmp_path}" in caplog.text
        assert "Found 4 track YAML file(s) (dry-run)" in caplog.text
        assert "Processing file 1/4" in caplog.text
        if path == paths[2]:
            assert f"Processing file 3/4; 0 would change, 0 failed: {path}" in caplog.text
        return False

    monkeypatch.setattr("mediascripts.track.track_yaml.track_yaml_reformat.reformat_track_yaml", unchanged)
    with caplog.at_level(logging.INFO):
        assert main() == 0

    assert sum("Processing file" in message for message in caplog.messages) == 2
    assert "Processed 4 track YAML file(s); 0 would change, 0 failed" in caplog.text
    assert not list(tmp_path.glob("*.bak"))


@pytest.mark.parametrize("media_key", ["mediaFileData", "media_file_data", "mediafile_data"])
@pytest.mark.parametrize("acoustid_key", ["acoustidData", "acoustid_data"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_acoustid_recordings_are_repaired(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    media_key: str, acoustid_key: str, dry_run: bool,
) -> None:
    path = tmp_path / "05 - BIG 5.yml"
    original = (
        '# Header\ntrackData:\n  title: "BIG 5"\n'
        f"{media_key}:\n"
        f"  - {acoustid_key}:\n"
        "      status: ok\n"
        "      results:\n"
        "        - id: ce5c6d17-e019-4f29-a57d-baf821410c44 # keep\n"
        "          score: 0.99709123\n"
        "        - id: existing\n"
        "          score: 0.5\n"
        "          recordings:\n"
        "            - id: recording-id\n"
    )
    path.write_text(original, encoding="utf-8")

    assert reformat_track_yaml(path, dry_run=dry_run)

    backup = path.with_name(f"{path.name}.bak")
    if dry_run:
        assert any(
            line.startswith("+") and line[1:].strip() == "recordings: []"
            for line in capsys.readouterr().out.splitlines()
        )
        assert path.read_text(encoding="utf-8") == original
        assert not backup.exists()
    else:
        content = path.read_text(encoding="utf-8")
        results = create_yaml().load(content)[media_key][0][acoustid_key]["results"]
        assert results[0] == {
            "id": "ce5c6d17-e019-4f29-a57d-baf821410c44", "score": 0.99709123, "recordings": [],
        }
        assert results[1]["recordings"] == [{"id": "recording-id"}]
        assert "# Header" in content and "# keep" in content and '"BIG 5"' in content
        assert backup.read_text(encoding="utf-8") == original
        assert not reformat_track_yaml(path)
        assert backup.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("recordings", ["[]", "null", "[{id: existing}]"])
def test_existing_acoustid_recordings_are_not_replaced(tmp_path: Path, recordings: str) -> None:
    path = tmp_path / "song.yml"
    original = (
        "mediaFileData:\n  - acoustidData:\n      results:\n"
        f"        - id: existing\n          recordings: {recordings}\n"
        "trackIdData:\n  acoustidData:\n    results:\n      - id: unrelated\n"
    )
    path.write_text(original, encoding="utf-8")

    reformat_track_yaml(path)

    document = create_yaml().load(path.read_text(encoding="utf-8"))
    assert document["mediaFileData"][0]["acoustidData"]["results"][0]["recordings"] == create_yaml().load(recordings)
    assert document["trackIdData"]["acoustidData"]["results"] == [{"id": "unrelated"}]


def test_acoustid_repair_preserves_aliases(tmp_path: Path) -> None:
    path = tmp_path / "song.yml"
    path.write_text(
        "mediaFileData:\n  - acoustidData:\n      results:\n"
        "        - &result {id: shared, score: 0.9}\n"
        "        - *result\n",
        encoding="utf-8",
    )

    assert reformat_track_yaml(path)

    results = create_yaml().load(path.read_text(encoding="utf-8"))["mediaFileData"][0]["acoustidData"]["results"]
    assert results[0] is results[1]
    assert results[0]["recordings"] == []


def test_count_includes_acoustid_only_repairs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = "mediaFileData:\n  - acoustidData:\n      results:\n        - id: missing\n"
    (tmp_path / "01-no-change.yml").write_text("trackData:\n  title: Unchanged\n", encoding="utf-8")
    first = tmp_path / "02-repair.yml"
    second = tmp_path / "03-repair.yml"
    for path in (first, second):
        path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["track-yaml-reformat", "--dir", str(tmp_path), "--count", "1"])

    assert main() == 0

    assert "recordings: []" in first.read_text(encoding="utf-8")
    assert second.read_text(encoding="utf-8") == original


def test_recursive_alias_is_not_corrupted(tmp_path: Path) -> None:
    path = tmp_path / "song.yml"
    original = "x: &x [*x]\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="content or aliases would change"):
        reformat_track_yaml(path)
    assert path.read_text(encoding="utf-8") == original


def test_comments_are_not_lost(tmp_path: Path) -> None:
    path = tmp_path / "song.yml"
    original = "x: {a: 1, # first\n b: 2} # end\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="comments would change"):
        reformat_track_yaml(path)
    assert path.read_text(encoding="utf-8") == original


def test_merge_keys_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "song.yml"
    path.write_text("defaults: &defaults {a: 1, b: 2}\nx: {<<: *defaults, c: 3}\n", encoding="utf-8")

    assert reformat_track_yaml(path)
    result = path.read_text(encoding="utf-8")
    assert "<<: *defaults" in result
    assert_block_collections(create_yaml().compose(result))


def test_flow_mapping_keys_and_sets_are_reformatted(tmp_path: Path) -> None:
    path = tmp_path / "song.yml"
    path.write_text("? [one, two]\n: !!set {first: null, second: null}\n", encoding="utf-8")

    assert reformat_track_yaml(path)
    assert_block_collections(create_yaml().compose(path.read_text(encoding="utf-8")))


def test_directory_cli_excludes_metadata_and_continues_after_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    good = tmp_path / "01 - Song.yaml"
    bad = tmp_path / "02 - Broken.yml"
    artist = tmp_path / "artist.yml"
    good.write_text("trackData: {title: Song}\n", encoding="utf-8")
    bad.write_text("trackData: [broken\n", encoding="utf-8")
    artist.write_text("artistData: {name: Artist}\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["track-yaml-reformat", "--dir", str(tmp_path)])

    with caplog.at_level(logging.INFO):
        assert main() == 1
    assert "title: Song" in good.read_text(encoding="utf-8")
    assert_block_collections(create_yaml().compose(good.read_text(encoding="utf-8")))
    assert artist.read_text(encoding="utf-8") == "artistData: {name: Artist}\n"
    assert "Could not reformat" in caplog.text and str(bad) in caplog.text
    assert "Updated " not in caplog.text
    assert shlex.join(["meld", str(good.with_name(f"{good.name}.bak").resolve()), str(good.resolve())]) in caplog.text


def test_dry_run_and_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    first = tmp_path / "01.yml"
    second = tmp_path / "02.yml"
    for path in (first, second):
        path.write_text("x: [one, two]\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["track-yaml-reformat", "-d", str(tmp_path), "-c", "1", "-n"])

    assert main() == 0
    assert str(first) in capsys.readouterr().out
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8") == "x: [one, two]\n"


def test_count_ignores_files_that_need_no_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture,
) -> None:
    no_change = tmp_path / "01-no-change.yml"
    first_change = tmp_path / "02-first-change.yml"
    later_change = tmp_path / "03-later-change.yml"
    no_change.write_text("x:\n  - one\n  - two\n", encoding="utf-8")
    first_change.write_text("x: [one, two]\n", encoding="utf-8")
    later_change.write_text("x: [three, four]\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["track-yaml-reformat", "--dir", str(tmp_path), "-c", "1", "-n"])

    with caplog.at_level(logging.INFO):
        assert main() == 0

    output = capsys.readouterr().out
    assert str(first_change) in output
    assert str(later_change) not in output
    assert "Processed 2 track YAML file(s); 1 would change, 0 failed" in caplog.text
    assert no_change.read_text(encoding="utf-8") == "x:\n  - one\n  - two\n"
    assert first_change.read_text(encoding="utf-8") == "x: [one, two]\n"
    assert later_change.read_text(encoding="utf-8") == "x: [three, four]\n"


def test_cli_exits_cleanly_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "01.yml"
    path.write_text("x: [one, two]\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["track-yaml-reformat", "--file", str(path)])

    def interrupt(*args: object, **kwargs: object) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr("mediascripts.track.track_yaml.track_yaml_reformat.reformat_track_yaml", interrupt)
    with caplog.at_level(logging.INFO):
        assert main() == 130
    assert "Interrupted by user; exiting" in caplog.text


def test_symlink_file_preserved(tmp_path: Path) -> None:
    target = tmp_path / "target.yml"
    link = tmp_path / "link.yml"
    target.write_text("x: [one, two]\n", encoding="utf-8")
    os.symlink(target, link)

    assert reformat_track_yaml(link)
    assert link.is_symlink()
    assert_block_collections(create_yaml().compose(target.read_text(encoding="utf-8")))


def test_default_collection_scans_album_directories_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    album = tmp_path / "data" / "Music" / "Artist" / "Album [2020]"
    album.mkdir(parents=True)
    track = album / "Song.yml"
    outside = tmp_path / "data" / "unrelated.yml"
    track.write_text("x: [one, two]\n", encoding="utf-8")
    outside.write_text("x: [one, two]\n", encoding="utf-8")
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["track-yaml-reformat"])

    assert main() == 0
    assert_block_collections(create_yaml().compose(track.read_text(encoding="utf-8")))
    assert outside.read_text(encoding="utf-8") == "x: [one, two]\n"
