import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from mediascripts.mediascan_db.list_albums import main as list_albums
from mediascripts.mediascan_db.list_artists import main as list_artists
from mediascripts.mediascan_db.list_mediafiles import main as list_mediafiles


@pytest.fixture
def collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "collection with spaces"
    root.mkdir()
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", str(root))
    monkeypatch.setattr(sys, "argv", ["list-table"])
    return root


@pytest.mark.parametrize(
    ("main", "table"),
    [(list_artists, "artist"), (list_albums, "album"), (list_mediafiles, "mediafile")],
)
def test_lists_entire_table(
    collection: Path,
    main: Callable[[], None],
    table: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = collection / "mediascan.db"
    # Many rows and long values expose accidental truncation in the printed table.
    long_value = "value_" + "x" * 100
    with sqlite3.connect(db_path) as connection:
        for name in ("artist", "album", "mediafile"):
            connection.execute(f"CREATE TABLE {name} (id INTEGER, name TEXT, details TEXT)")
            # :03d formats an integer with at least three digits, e.g. artist_000.
            # Each ? is a SQL parameter filled from the corresponding tuple.
            connection.executemany(
                f"INSERT INTO {name} VALUES (?, ?, ?)",
                [(index, f"{name}_{index:03d}", long_value) for index in range(75)],
            )
    # Compare the database bytes afterward to ensure listing did not change it.
    original = db_path.read_bytes()

    main()

    output = capsys.readouterr().out
    assert output.splitlines()[0].split() == ["id", "name", "details"]
    assert len(output.splitlines()) == 76
    for index in range(75):
        assert f"{table}_{index:03d}" in output
    assert output.count(long_value) == 75
    assert "..." not in output
    assert db_path.read_bytes() == original


def test_relative_collection_root(
    collection: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with sqlite3.connect(collection / "mediascan.db") as connection:
        connection.execute("CREATE TABLE artist (name TEXT)")
        connection.execute("INSERT INTO artist VALUES ('Example artist')")
    monkeypatch.chdir(collection.parent)
    monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", collection.name)

    list_artists()

    assert "Example artist" in capsys.readouterr().out


@pytest.mark.parametrize("value", [None, ""])
def test_requires_collection_root(
    collection: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
) -> None:
    monkeypatch.delenv("MOONGAS_COLLECTION_ROOTDIR")
    if value is not None:
        monkeypatch.setenv("MOONGAS_COLLECTION_ROOTDIR", value)

    with pytest.raises(SystemExit) as error:
        list_artists()

    assert error.value.code == 2
    assert "MOONGAS_COLLECTION_ROOTDIR must be set" in capsys.readouterr().err
    assert not (collection / "mediascan.db").exists()


def test_missing_database_is_not_created(collection: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        list_albums()

    assert error.value.code == 2
    assert "database file not found" in capsys.readouterr().err
    assert not (collection / "mediascan.db").exists()


def test_missing_table_is_reported(collection: Path) -> None:
    with sqlite3.connect(collection / "mediascan.db"):
        pass

    with pytest.raises(ValueError, match="Table album not found"):
        list_albums()


def test_lists_all_columns(collection: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # :02d pads integers to two digits: column_00, column_01, ..., column_29.
    # Thirty columns make this table wide enough to expose column truncation.
    columns = [f"column_{index:02d}" for index in range(30)]
    with sqlite3.connect(collection / "mediascan.db") as connection:
        # Join definitions into SQL such as "column_00 TEXT, column_01 TEXT".
        connection.execute(f"CREATE TABLE album ({', '.join(f'{column} TEXT' for column in columns)})")
        # Generate one ? per column; _ means the loop's column value is unused.
        # The value list supplies matching cells: value_00, value_01, ..., value_29.
        connection.execute(
            f"INSERT INTO album VALUES ({', '.join('?' for _ in columns)})",
            [f"value_{index:02d}" for index in range(30)],
        )

    list_albums()

    # capsys captures stdout; expect a header line followed by one complete row.
    output = capsys.readouterr().out.splitlines()
    assert output[0].split() == columns
    assert output[1].split() == [f"value_{index:02d}" for index in range(30)]


def test_empty_table(collection: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with sqlite3.connect(collection / "mediascan.db") as connection:
        connection.execute("CREATE TABLE mediafile (id INTEGER, title TEXT)")

    list_mediafiles()

    output = capsys.readouterr().out
    assert "Empty DataFrame" in output
    assert "id" in output
    assert "title" in output


@pytest.mark.parametrize(
    ("main", "table"),
    [(list_artists, "artist"), (list_albums, "album"), (list_mediafiles, "mediafile")],
)
def test_left_aligns_headers_and_values(
    collection: Path,
    main: Callable[[], None],
    table: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with sqlite3.connect(collection / "mediascan.db") as connection:
        connection.execute(f"CREATE TABLE {table} (id INTEGER, path TEXT, name TEXT, year INTEGER, explicitcover BOOLEAN)")
        connection.executemany(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?)",
            [(1, "/short", "A", 2005, False), (123, "/longer/path", "Long album", 1594, True)],
        )

    main()

    lines = capsys.readouterr().out.splitlines()
    expected_rows = [
        ["id", "path", "name", "year", "explicitcover"],
        ["1", "/short", "A", "2005", "False"],
        ["123", "/longer/path", "Long album", "1594", "True"],
    ]
    # Each cell must start at its column's left edge, including numbers and booleans.
    widths = [3, 12, 10, 4, 13]
    assert len(lines) == len(expected_rows)
    for line, cells in zip(lines, expected_rows):
        offset = 0
        for cell, width in zip(cells, widths):
            assert line[offset : offset + width] == cell.ljust(width)
            offset += width + 1


def test_help_does_not_require_collection(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("MOONGAS_COLLECTION_ROOTDIR", raising=False)
    monkeypatch.setattr(sys, "argv", ["list-mediafiles", "--help"])

    with pytest.raises(SystemExit) as error:
        list_mediafiles()

    assert error.value.code == 0
    assert "mediafile table" in capsys.readouterr().out
