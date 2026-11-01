import logging
import os
import sys
from pathlib import Path

import pytest

from mediascripts.convert.convert_covers import convert_medialibs_cover_images_inplace
from mediascripts.copy.copy_medialib import (
    DirCopyMode,
    copy_medialib,
    copy_medialibs,
    parse_args,
)


def test_copy_covers(tmp_path: Path) -> None:
    src_path = tmp_path / "src"
    dst_path = tmp_path / "dst"
    copy_medialib(
        src_path,
        dst_path,
        include_filenames=["cover.jpg"],
        exclude_keywords=["Sepulga"],
        dry_run=True,
        overwrite_existing=True,
        dir_copy_mode=DirCopyMode.PreserveStructure,
    )


def test_copy_all(tmp_path: Path):
    src_path1 = tmp_path / "src" / "lib1"
    src_path2 = tmp_path / "src" / "lib2"
    dst_path = tmp_path / "dst"
    copy_medialibs([src_path1, src_path2], dst_path)


@pytest.mark.parametrize(
    ("keywords", "expected_copies", "excluded_count"),
    [([], 1, 0), ([""], 0, 1), (["album"], 0, 1)],
)
def test_copy_medialib_logs_include_and_exclude_decisions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    keywords: list[str], expected_copies: int, excluded_count: int,
) -> None:
    src_path = tmp_path / "src"
    src_path.mkdir()
    (src_path / "album.yml").write_text("title: Album\n")
    (src_path / "song.mp3").write_bytes(b"audio")

    with caplog.at_level(logging.INFO):
        result = copy_medialib(
            src_path, tmp_path / "dst", include_filenames=["album.yml"],
            exclude_keywords=keywords, dry_run=True,
        )

    assert result.would_be_copied == expected_copies
    assert result.skipped == 2 - expected_copies
    assert "include_filenames=['album.yml']" in caplog.text
    assert f"exclude_keywords={keywords!r}" in caplog.text
    assert "matched include patterns=1" in caplog.text
    assert "filename_not_included=1" in caplog.text
    assert f"excluded_keyword={excluded_count}" in caplog.text
    if keywords == [""]:
        assert "excludes every included file" in caplog.text
        assert "based on exclude keyword ''" in caplog.text


def test_copy_medialib_does_not_overwrite_newer_destination(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    src_path = tmp_path / "src"
    dst_path = tmp_path / "dst"
    src_path.mkdir()
    dst_path.mkdir()
    src_file = src_path / "cover.jpg"
    dst_file = dst_path / "cover.jpg"
    src_file.write_text("source")
    dst_file.write_text("newer destination")
    os.utime(src_file, (100, 100))
    os.utime(dst_file, (200, 200))

    with caplog.at_level(logging.INFO):
        count = copy_medialib(
            src_path,
            dst_path,
            include_filenames=["cover.jpg"],
            overwrite_existing=False,
        )

    assert count.skipped == 1
    assert count.actually_copied == 0
    assert dst_file.read_text() == "newer destination"
    assert (
        f"Skipping file '{src_file}' because the destination file '{dst_file}' is newer"
        in caplog.text
    )
    assert "newer_destination=1" in caplog.text
    assert "--overwrite-existing does not replace newer files" in caplog.text


def test_copy_medialib_overwrites_older_destination(tmp_path: Path) -> None:
    src_path = tmp_path / "src"
    dst_path = tmp_path / "dst"
    src_path.mkdir()
    dst_path.mkdir()
    src_file = src_path / "cover.jpg"
    dst_file = dst_path / "cover.jpg"
    src_file.write_text("source")
    dst_file.write_text("older destination")
    os.utime(src_file, (200, 200))
    os.utime(dst_file, (100, 100))

    count = copy_medialib(
        src_path,
        dst_path,
        include_filenames=["cover.jpg"],
        overwrite_existing=True,
    )

    assert count.actually_copied == 1
    assert count.skipped == 0
    assert dst_file.read_text() == "source"


def test_copy_medialib_skips_identical_destination(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    src_path = tmp_path / "src"
    dst_path = tmp_path / "dst"
    src_path.mkdir()
    dst_path.mkdir()
    src_file = src_path / "cover.jpg"
    dst_file = dst_path / "cover.jpg"
    src_file.write_text("identical content")
    dst_file.write_text("identical content")
    os.utime(src_file, (100, 100))
    os.utime(dst_file, (200, 200))

    with caplog.at_level(logging.DEBUG):
        result = copy_medialib(
            src_path,
            dst_path,
            include_filenames=["cover.jpg"],
            overwrite_existing=True,
            overwrite_newer=False,
        )

    assert result.actually_copied == 0
    assert result.skipped == 1
    assert (
        f"Skipping file '{src_file}' because it is identical to destination file "
        f"'{dst_file}'" in caplog.text
    )


def test_copy_medialib_overwrites_newer_destination_when_requested(
    tmp_path: Path,
) -> None:
    src_path = tmp_path / "src"
    dst_path = tmp_path / "dst"
    src_path.mkdir()
    dst_path.mkdir()
    src_file = src_path / "cover.jpg"
    dst_file = dst_path / "cover.jpg"
    src_file.write_text("source")
    dst_file.write_text("newer destination")
    os.utime(src_file, (100, 100))
    os.utime(dst_file, (200, 200))

    result = copy_medialib(
        src_path,
        dst_path,
        include_filenames=["cover.jpg"],
        overwrite_newer=True,
    )

    assert result.actually_copied == 1
    assert result.skipped == 0
    assert dst_file.read_text() == "source"


def test_copy_functions_reject_conflicting_overwrite_options(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        copy_medialib(
            tmp_path / "src",
            tmp_path / "dst",
            overwrite_existing=True,
            overwrite_newer=True,
        )

    with pytest.raises(ValueError, match="mutually exclusive"):
        copy_medialibs(
            [],
            tmp_path / "dst",
            overwrite_existing=True,
            overwrite_newer=True,
        )


def test_parse_args_rejects_conflicting_overwrite_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "copy_medialib",
            "--dst-path",
            str(tmp_path / "dst"),
            "--overwrite-existing",
            "--overwrite-newer",
        ],
    )

    with pytest.raises(SystemExit):
        parse_args()


def test_convert_all(tmp_path: Path):
    src_path1 = tmp_path / "src" / "lib1"
    src_path2 = tmp_path / "src" / "lib2"
    convert_medialibs_cover_images_inplace([src_path1, src_path2])
