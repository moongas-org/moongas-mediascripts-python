from io import StringIO

import pytest
from ruamel.yaml import YAML

from mediascripts.acoustid.recording_yaml import load_track, normalize_isrcs


def document(value: object) -> dict:
    return {
        "trackData": {},
        "trackIdData": [
            {
                "acoustidData": {
                    "results": [
                        {
                            "recordings": [
                                {"id": "one", "isrcs": value},
                                {"id": "two", "isrcs": value},
                            ]
                        }
                    ]
                }
            }
        ],
    }


def test_shared_list_serializes_without_aliases_or_forced_quotes() -> None:
    data = document(["GBAAA0500078"])
    assert normalize_isrcs(data)
    stream = StringIO()
    YAML().dump(data, stream)
    text = stream.getvalue()
    assert text.count("- GBAAA0500078") == 2
    assert '"GBAAA0500078"' not in text
    assert "&id" not in text and "*id" not in text
    yaml = YAML()
    assert not normalize_isrcs(yaml.load(text))


@pytest.mark.parametrize("value", ["*id001", "&id001", None, 123, {"bad": "value"}, [12]])
def test_invalid_values_are_reset(value: object) -> None:
    data = document(value)
    normalize_isrcs(data)
    assert data["trackIdData"][0]["acoustidData"]["results"][0]["recordings"][0]["isrcs"] == []


def test_existing_aliases_are_expanded() -> None:
    stream = StringIO()
    YAML().dump(document(["GBAAA0500078"]), stream)
    assert "&id" in stream.getvalue()
    data = YAML().load(stream.getvalue())
    assert normalize_isrcs(data)
    output = StringIO()
    YAML().dump(data, output)
    assert "&id" not in output.getvalue()
    assert "*id" not in output.getvalue()


def test_block_sequence_comments_are_preserved() -> None:
    text = (
        "trackData: {}\ntrackIdData:\n- acoustidData:\n    results:\n    - recordings:\n"
        "      - id: x\n        isrcs: # collection note\n        - GBAAA0500078 # recording note\n"
    )
    yaml = YAML()
    data = yaml.load(text)
    assert not normalize_isrcs(data)
    output = StringIO()
    yaml.dump(data, output)
    assert "# collection note" in output.getvalue()
    assert "# recording note" in output.getvalue()
    assert "- GBAAA0500078" in output.getvalue()


def test_unquoted_string_lists_are_not_flagged_for_repair() -> None:
    data = YAML().load("trackIdData: [{acoustidData: {results: [{recordings: [{id: one, isrcs: [GBAAA0500078]}]}]}}]")
    assert not normalize_isrcs(data)


def test_yaml_handles_strings_that_need_quoting() -> None:
    data = document(["123", "true"])
    normalize_isrcs(data)
    output = StringIO()
    YAML().dump(data, output)
    assert YAML(typ="safe").load(output.getvalue()) == data


@pytest.mark.parametrize("dry_run", [True, False])
def test_bad_yaml_restores_valid_backup(tmp_path, dry_run: bool) -> None:
    path = tmp_path / "track.yml"
    bad = "trackData: {}\nisrcs: *undefined\n"
    good = "trackData: {}\ntrackIdData: []\n"
    path.write_text(bad)
    backup = path.with_name(path.name + ".bak")
    backup.write_text(good)
    original, data, recovered = load_track(path, dry_run=dry_run)
    assert recovered and data["trackData"] == {}
    assert path.read_text() == (bad if dry_run else good)
    assert original == path.read_text()
    assert backup.read_text() == good


def test_bad_backup_does_not_replace_source(tmp_path) -> None:
    path = tmp_path / "track.yml"
    path.write_text("trackData: *missing\n")
    path.with_name(path.name + ".bak").write_text("[]")
    with pytest.raises(ValueError, match="Backup is not a track"):
        load_track(path, dry_run=False)
    assert path.read_text() == "trackData: *missing\n"
