import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from mediascripts.copy import copy_medialib as copy_module
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


@pytest.mark.parametrize("make_track_yml", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_copy_creates_parent_for_source_added_after_directory_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_track_yml: bool, dry_run: bool,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    real_walk = os.walk
    walk_count = 0

    def walk_with_new_album(path: Path, topdown: bool = True) -> Iterator[tuple[str, list[str], list[str]]]:
        nonlocal walk_count
        walk_count += 1
        yield from real_walk(path, topdown=topdown)
        if walk_count == 1:
            album = source / "The Doors" / "The Very Best of The Doors [2008]"
            album.mkdir(parents=True)
            (album / "01 - Break On Through.yml").write_text("trackData: {}\n")

    monkeypatch.setattr(copy_module.os, "walk", walk_with_new_album)
    result = copy_medialib(
        source, destination, include_filenames=["*.yml"], dry_run=dry_run,
        make_track_yml=make_track_yml,
    )
    output = destination / "The Doors" / "The Very Best of The Doors [2008]" / "01 - Break On Through.yml"
    assert result.would_be_copied == 1
    assert result.actually_copied == (0 if dry_run else 1)
    if dry_run:
        assert not destination.exists()
    else:
        assert output.read_text() == ("" if make_track_yml else "trackData: {}\n")


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


@pytest.mark.parametrize("delete", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_delete_matches_filters_and_preserves_source_counterparts(
    tmp_path: Path, delete: bool, dry_run: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "keep.yml").write_text("keep")
    (destination / "keep.yml").write_text("keep")
    (destination / "extra.yml").write_text("extra")
    (destination / "notes.txt").write_text("unmatched")
    excluded = destination / "Excluded"
    excluded.mkdir()
    (excluded / "extra.yml").write_text("excluded")
    removed_album = destination / "Removed Album"
    removed_album.mkdir()
    (removed_album / "old.yml").write_text("old")

    with caplog.at_level(logging.INFO):
        result = copy_medialib(
            source, destination, include_filenames=["*.yml"], exclude_keywords=["Excluded"],
            delete=delete, dry_run=dry_run,
        )

    assert (destination / "keep.yml").read_text() == "keep"
    assert (destination / "notes.txt").read_text() == "unmatched"
    assert (excluded / "extra.yml").read_text() == "excluded"
    assert (destination / "extra.yml").exists() == (not delete or dry_run)
    assert (removed_album / "old.yml").exists() == (not delete or dry_run)
    assert removed_album.is_dir()
    assert result.would_be_deleted == (2 if delete else 0)
    assert result.actually_deleted == (2 if delete and not dry_run else 0)
    if delete and dry_run:
        assert "Would delete" in caplog.text


def test_delete_preserves_counterparts_skipped_by_overwrite_rules(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    for name in ("existing.yml", "newer.yml"):
        (source / name).write_text("source")
        (destination / name).write_text("destination")
        os.utime(source / name, (100, 100))
    os.utime(destination / "existing.yml", (50, 50))
    os.utime(destination / "newer.yml", (200, 200))

    result = copy_medialib(source, destination, include_filenames=["*.yml"], delete=True)

    assert result.would_be_deleted == 0
    assert all((destination / name).read_text() == "destination" for name in ("existing.yml", "newer.yml"))


def test_delete_empty_exclusion_protects_everything(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    extra = destination / "extra.yml"
    extra.touch()

    result = copy_medialib(
        source, destination, include_filenames=["*.yml"], exclude_keywords=[""], delete=True,
    )

    assert extra.exists()
    assert result.actually_deleted == 0


@pytest.mark.parametrize("dry_run", [False, True])
def test_delete_track_yaml_uses_generated_destination_names(tmp_path: Path, dry_run: bool) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "keep.lrc").write_text("lyrics")
    (destination / "keep.yml").write_text("metadata")
    (destination / "extra.yml").write_text("extra metadata")
    (destination / "extra.lrc").write_text("unmatched destination")

    result = copy_medialib(
        source, destination, include_filenames=["*.lrc"], make_track_yml=True,
        delete=True, dry_run=dry_run,
    )

    assert (destination / "keep.yml").read_text() == "metadata"
    assert (destination / "extra.yml").exists() == dry_run
    assert (destination / "extra.lrc").exists()
    assert result.would_be_deleted == 1


@pytest.mark.parametrize("mode", ["missing", "same", "nested", "parent", "flat"])
def test_delete_rejects_unsafe_paths_and_modes(tmp_path: Path, mode: str) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    marker = destination / "keep.yml"
    marker.write_text("keep")
    copy_mode = DirCopyMode.PreserveStructure
    if mode == "missing":
        source = tmp_path / "missing"
    elif mode == "same":
        source = destination
    elif mode == "nested":
        source = tmp_path
    elif mode == "parent":
        destination = tmp_path
    else:
        copy_mode = DirCopyMode.SingleDirectory

    with pytest.raises(ValueError):
        copy_medialib(source, destination, delete=True, dir_copy_mode=copy_mode)

    assert marker.read_text() == "keep"


def test_delete_scan_errors_prevent_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    extra = destination / "extra.yml"
    extra.touch()
    real_walk = os.walk

    def failing_walk(path: Path, **kwargs):
        if kwargs.get("onerror") is not None:
            kwargs["onerror"](PermissionError("cannot scan"))
        yield from real_walk(path, **kwargs)

    monkeypatch.setattr(copy_module.os, "walk", failing_walk)

    with pytest.raises(PermissionError, match="cannot scan"):
        copy_medialib(source, destination, include_filenames=["*.yml"], delete=True)

    assert extra.exists()


def test_delete_does_not_follow_destination_directory_symlinks(tmp_path: Path) -> None:
    source, destination, outside = tmp_path / "source", tmp_path / "destination", tmp_path / "outside"
    source.mkdir()
    destination.mkdir()
    outside.mkdir()
    (outside / "extra.yml").touch()
    (destination / "linked").symlink_to(outside, target_is_directory=True)
    (destination / "extra.yml").symlink_to(outside / "extra.yml")

    result = copy_medialib(source, destination, include_filenames=["*.yml"], delete=True)

    assert (outside / "extra.yml").exists()
    assert (destination / "linked").is_symlink()
    assert not (destination / "extra.yml").is_symlink()
    assert result.actually_deleted == 1


def test_delete_multiple_libraries_aggregates_results(tmp_path: Path) -> None:
    libraries = [tmp_path / "source" / name for name in ("Music", "Other")]
    destination = tmp_path / "destination"
    for library in libraries:
        library.mkdir(parents=True)
        target = destination / library.name
        target.mkdir(parents=True)
        (target / "extra.yml").touch()

    result = copy_medialibs(libraries, destination, include_filenames=["*.yml"], delete=True)

    assert result.would_be_deleted == result.actually_deleted == 2
    assert not list(destination.rglob("*.yml"))


@pytest.mark.parametrize("make_track_yml", [False, True])
def test_delete_protects_unscanned_source_directory_symlinks(tmp_path: Path, make_track_yml: bool) -> None:
    source, destination, outside = tmp_path / "source", tmp_path / "destination", tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (source / "linked").symlink_to(outside, target_is_directory=True)
    (destination / "linked").mkdir(parents=True)
    marker = destination / "linked" / "keep.yml"
    marker.write_text("protected")

    result = copy_medialib(
        source, destination, include_filenames=["*.lrc" if make_track_yml else "*.yml"],
        make_track_yml=make_track_yml, delete=True,
    )

    assert marker.read_text() == "protected"
    assert result.actually_deleted == 0


def test_delete_validates_all_sources_before_removing_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    (destination / "source").mkdir(parents=True)
    marker = destination / "source" / "extra.yml"
    marker.touch()

    with pytest.raises(ValueError, match="missing or invalid source"):
        copy_medialibs(
            [source, tmp_path / "missing"], destination, include_filenames=["*.yml"], delete=True,
        )

    assert marker.exists()


def test_parse_args_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["copy-medialib", "--dst-path", str(tmp_path), "--delete", "--dry-run"])
    args = parse_args()
    assert args.delete
    assert args.dry_run
