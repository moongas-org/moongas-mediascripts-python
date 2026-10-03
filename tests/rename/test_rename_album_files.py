import os

import pytest

from mediascripts.rename.rename_album_files import update_filename


def test_update_filename_applies_filename_cleanup_rules() -> None:
    filename = "03 - Lil Wayne - MegaMan [Official Music Video] 🔄.mp3"

    assert update_filename(filename, "Lil Wayne") == "03 - MegaMan.mp3"


def test_update_filename_normalizes_artist_and_featured_artist() -> None:
    filename = "04 - Jay‐Z feat. Rick Ross - FuckWithMeYouKnowIGotIt.mp3"

    assert (
        update_filename(filename, "Jay-Z")
        == "04 - Jay-Z ft Rick Ross - FuckWithMeYouKnowIGotIt.mp3"
    )


@pytest.mark.parametrize(
    "filename, artist, expected",
    [
        ("01.02 - I Found Love.yml", "X", "01.02 - I Found Love.yml"),
        ("01.02 - I Found Love.flac", "X", "01.02 - I Found Love.flac"),
        ("U2 - I Still Haven't Found.lrc", "U2", "I Still Haven't Found.lrc"),
        (
            "Men I Trust - Oncle Jazz - 05 Found Me.yml",
            "Men I Trust",
            "Oncle Jazz - 05 Found Me.yml",
        ),
        ("A & B – Song.lrc", "A and B", "Song.lrc"),
        ("01 - Song (Official Video).mp3", "X", "01 - Song.mp3"),
        ("01 - Song (Official HD Video).mp3", "X", "01 - Song.mp3"),
        ("01 - Song (official lyric video).mp3", "X", "01 - Song.mp3"),
        ("01 - Song (Audio).mp3", "X", "01 - Song.mp3"),
        ("01 - Song (Remastered).mp3", "X", "01 - Song (Remastered).mp3"),
        ("01 - Song Feat. A.mp3", "X", "01 - Song ft A.mp3"),
        ("01 - Song (FEAT A).mp3", "X", "01 - Song (ft A).mp3"),
        ("01 - Song Ft_ A.mp3", "X", "01 - Song ft A.mp3"),
        ("01 - Song featuring A.mp3", "X", "01 - Song ft A.mp3"),
        ("01 - Daft Creature Defeat.mp3", "X", "01 - Daft Creature Defeat.mp3"),
    ],
)
def test_update_filename_cases(filename: str, artist: str, expected: str) -> None:
    assert update_filename(filename, artist) == expected


def test_update_filename_renames_sidecars_consistently() -> None:
    stems = {
        os.path.splitext(
            update_filename(f"03 - Lil Wayne - MegaMan (feat. X){ext}", "Lil Wayne")
        )[0]
        for ext in (".mp3", ".flac", ".yml", ".lrc", ".txt")
    }
    assert stems == {"03 - MegaMan (ft X)"}
