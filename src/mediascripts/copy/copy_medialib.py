import argparse
import fnmatch
import hashlib
import logging
import os
import shlex
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from tqdm import tqdm

from mediascan.utils.log.log_arguments import log_arguments

log = logging.getLogger(__name__)


class DirCopyMode(Enum):
    SingleDirectory = 1  #  All images copies to single destination directory, replacing filenames with 00001.jpg etc.
    PreserveStructure = 2  #  Preserve directory structure and filenames in destination


class CopyResults(NamedTuple):
    would_be_copied: int
    actually_copied: int
    skipped: int
    errors: int


def _files_are_identical(first_path: Path, second_path: Path) -> bool:
    """Return True if both files have identical content.

    Fast path: if the file sizes differ the files cannot be identical, so the
    (expensive) content hash is only computed when sizes match. This makes
    incremental runs over an existing destination much faster, since files that
    changed are detected via stat() alone without re-reading their contents.
    """
    if first_path.stat().st_size != second_path.stat().st_size:
        return False

    def file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    return file_hash(first_path) == file_hash(second_path)


def _log_pending_change(src_path: Path, dst_path: Path, diff: bool = False) -> None:
    """Log a copy/paste-able meld command at INFO and the diff at INFO if diff else DEBUG."""
    action = "modify" if dst_path.exists() else "create"
    log.info(
        f"{action}: meld {shlex.quote(str(src_path))} {shlex.quote(str(dst_path))}"
    )
    diff_level = logging.INFO if diff else logging.DEBUG
    if not log.isEnabledFor(diff_level):
        return
    diff_target = dst_path if dst_path.exists() else Path(os.devnull)
    try:
        result = subprocess.run(
            ["diff", "-u", str(diff_target), str(src_path)],
            capture_output=True,
            text=True,
            errors="replace",
        )
    except OSError as e:
        log.log(diff_level, f"Could not diff '{src_path}' and '{diff_target}': {e}")
        return
    if result.stdout:
        log.log(diff_level, f"Diff for '{dst_path}':\n{result.stdout}")
    if result.stderr:
        log.log(diff_level, f"diff stderr for '{dst_path}': {result.stderr.strip()}")


@log_arguments
def copy_medialib(
    src_path: Path,
    dst_path: Path,
    include_filenames: list[str] = ["artist.yml", "cover.jpg"],
    exclude_keywords: list[str] = [],
    dry_run: bool = False,
    dir_copy_mode: DirCopyMode = DirCopyMode.PreserveStructure,
    overwrite_existing: bool = False,
    overwrite_newer: bool = False,
    make_track_yml: bool = False,
    diff: bool = False,
) -> CopyResults:
    """
    Recursively copy files (e.g. album cover images) from src medialib directory
    to specified destination directory, preserving directory structure (default)

    src_path : path e.g. '/data/Music'
    dst_path : path e.g. '/data/Covers/Music'

    If make_track_yml is True, no files are copied; instead an empty .yml file
    is created in the destination for each included source file, with the same
    base name (e.g. '01 - Song.lrc' -> '01 - Song.yml').

    Dir Copy Modes:
        1. SingleDirectory
            All images copies to single destination directory, replacing filenames with 00001.jpg etc.
        2. PreserveStructure
            Preserve directory structure and filenames in destination

    The files to be copied are determined in a first pass (building a list of
    (src, dst) pairs and logging the count) before any copying begins.

    Returns number it copied (or would have copied if not dry_run)
    """
    if overwrite_newer and overwrite_existing:
        raise ValueError(
            "overwrite_newer and overwrite_existing are mutually exclusive"
        )
    if overwrite_newer:
        overwrite_existing = True

    ret = CopyResults(
        would_be_copied=0,
        actually_copied=0,
        skipped=0,
        errors=0,
    )
    if not dry_run:
        Path(dst_path).mkdir(parents=True, exist_ok=True)

    # first make the directories
    if dir_copy_mode == DirCopyMode.PreserveStructure:
        for root, dirs, files in os.walk(src_path, topdown=False):
            for sd in dirs:
                src_dir_abs_path = Path(root).joinpath(sd)
                src_dir_rel_path = src_dir_abs_path.relative_to(src_path)
                dst_abs_path = Path(dst_path).joinpath(src_dir_rel_path)
                if not dry_run:
                    dst_abs_path.mkdir(parents=True, exist_ok=True)

    # First pass: build the list of (src, dst) file pairs to copy,
    # applying all skip rules, before copying anything
    copy_list: list[tuple[Path, Path]] = []
    scan_progress = tqdm(
        desc=f"Scanning '{src_path}'",
        unit=" files",
        disable=not sys.stderr.isatty(),
    )
    for root, dirs, files in os.walk(src_path, topdown=False):
        for src_fname in files:
            scan_progress.update(1)
            src_file_abs_path = Path(root).joinpath(src_fname)
            # skip any files not matching at least one include filename glob pattern
            if not any(
                fnmatch.fnmatch(src_fname, pattern) for pattern in include_filenames
            ):
                log.debug(
                    f"Skipping file '{src_file_abs_path}' because its filename is not included"
                )
                ret = ret._replace(skipped=ret.skipped + 1)
                continue
            # skip files with any exclude keywords anywhere in the file path
            skip = False
            for keyword in exclude_keywords:
                if str(src_file_abs_path).find(keyword) != -1:
                    log.debug(
                        f"Skipping file '{src_file_abs_path}' based on exclude keyword '{keyword}'"
                    )
                    skip = True
            if skip:
                ret = ret._replace(skipped=ret.skipped + 1)
                continue
            _, src_ext = os.path.splitext(src_fname)

            src_file_rel_path = src_file_abs_path.relative_to(src_path)
            dst_abs_path = Path(dst_path)
            if dir_copy_mode == DirCopyMode.SingleDirectory:
                # This mode is for creating flat dir full of images, etc.
                # so we need to make the filenames unique
                dst_ext = ".yml" if make_track_yml else src_ext
                dst_fname: str = str(len(copy_list) + 1).rjust(5, "0") + dst_ext
                dst_abs_path = dst_abs_path.joinpath(dst_fname)
            elif dir_copy_mode == DirCopyMode.PreserveStructure:
                # This mode retains the original filename exactly
                dst_abs_path = dst_abs_path.joinpath(src_file_rel_path)
                if make_track_yml:
                    # strip the source extension and use .yml instead
                    dst_abs_path = dst_abs_path.with_suffix(".yml")

            if dst_abs_path.exists() and _files_are_identical(
                src_file_abs_path, dst_abs_path
            ):
                log.debug(
                    f"Skipping file '{src_file_abs_path}' because it is identical "
                    f"to destination file '{dst_abs_path}'"
                )
                ret = ret._replace(skipped=ret.skipped + 1)
                continue

            # Never replace a destination file that is newer than the source.
            if (
                not overwrite_newer
                and dst_abs_path.exists()
                and dst_abs_path.stat().st_mtime > src_file_abs_path.stat().st_mtime
            ):
                log.debug(
                    f"Skipping file '{src_file_abs_path}' because the destination file "
                    f"'{dst_abs_path}' is newer"
                )
                ret = ret._replace(skipped=ret.skipped + 1)
                continue

            # Don't copy if it exists unless overwrite_existing==True.
            if not overwrite_existing and dst_abs_path.exists():
                log.debug(
                    f"Skipping file '{src_file_abs_path}' because the destination file "
                    f"'{dst_abs_path}' already exists"
                )
                ret = ret._replace(skipped=ret.skipped + 1)
                continue

            # Special case:
            # If copying a .jpg file and .webp file already exists in destination,
            # skip the copy unless overwrite_existing==True.
            # (I copy jpegs first and then convert them to webp in place,
            # consequently I want to skip copying jpegs that have already been
            # converted)
            if not make_track_yml and src_ext == ".jpg" and not overwrite_existing:
                dst_fbase, _ = os.path.splitext(dst_abs_path)
                dst_abs_path_converted = Path(dst_fbase + ".webp")
                if dst_abs_path_converted.exists():
                    log.debug(
                        f"Skipping file '{src_file_abs_path}' because the converted "
                        f"destination file '{dst_abs_path_converted}' already exists"
                    )
                    ret = ret._replace(skipped=ret.skipped + 1)
                    continue

            copy_list.append((src_file_abs_path, dst_abs_path))

    scan_progress.close()
    log.info(
        f"{len(copy_list)} file(s) to copy from '{src_path}' to '{dst_path}'"
        f"{' (dry run)' if dry_run else ''}"
    )

    # Second pass: perform the copies
    for src_file_abs_path, dst_abs_path in tqdm(
        copy_list,
        desc=f"Copying to '{dst_path}'",
        unit=" files",
        disable=not sys.stderr.isatty(),
    ):
        _log_pending_change(src_file_abs_path, dst_abs_path, diff=diff)
        if not dry_run:
            if make_track_yml:
                dst_abs_path.write_text("")
            else:
                shutil.copy(src_file_abs_path, dst_abs_path)
            ret = ret._replace(actually_copied=ret.actually_copied + 1)
        ret = ret._replace(would_be_copied=ret.would_be_copied + 1)
    return ret


@log_arguments
def copy_medialibs(
    src_paths: list[Path],
    dst_root_path: Path,
    include_filenames: list[str] = ["artist.yml", "cover.jpg"],
    exclude_keywords: list[str] = [],
    dry_run: bool = False,
    dir_copy_mode: DirCopyMode = DirCopyMode.PreserveStructure,
    overwrite_existing: bool = False,
    overwrite_newer: bool = False,
    make_track_yml: bool = False,
    diff: bool = False,
) -> CopyResults:
    """
    Copies files* from one or more medialib directories from src directory to
    medialib directories inside the specified root destination directory

    *Which files are copied is determined by the arguments. The idea is to only copy
    specified files and ignore everything else.

    src_paths : paths to one or more media library directories to be copied from
    E.g. /data/Music and /data/OtherMusic

    dst_root_path : path to root destination directory medialibs will be copied into
    E.g. /data/Covers

    Complete example:
    Given the following arguments:
        - src_paths = ['/data/Music', '/data/OtherMusic']
        - dst_root_path = '/data/Covers'
    Then the following destination subdirectories would be created inside /data/Covers:
        - /data/Covers/Music
        - /data/Covers/OtherMusic
    """
    if overwrite_newer and overwrite_existing:
        raise ValueError(
            "overwrite_newer and overwrite-existing are mutually exclusive"
        )

    ret = CopyResults(
        would_be_copied=0,
        actually_copied=0,
        skipped=0,
        errors=0,
    )
    for src_path in src_paths:
        ret_tmp: CopyResults = copy_medialib(
            src_path=src_path,
            dst_path=dst_root_path.joinpath(src_path.name),
            include_filenames=include_filenames,
            dir_copy_mode=dir_copy_mode,
            exclude_keywords=exclude_keywords,
            dry_run=dry_run,
            overwrite_existing=overwrite_existing,
            overwrite_newer=overwrite_newer,
            make_track_yml=make_track_yml,
            diff=diff,
        )
        log.info(
            f"Total copied for medialib dir {src_path}: would be copied: {ret_tmp.would_be_copied}; actually copied: {ret_tmp.actually_copied}; skipped: {ret_tmp.skipped}; errors: {ret_tmp.errors};"
        )
        ret = ret._replace(
            would_be_copied=ret.would_be_copied + ret_tmp.would_be_copied,
            actually_copied=ret.actually_copied + ret_tmp.actually_copied,
            skipped=ret.skipped + ret_tmp.skipped,
            errors=ret.errors + ret_tmp.errors,
        )
    log.info(
        f"Grand total copied for all medialib dirs: would be copied: {ret.would_be_copied}; actually copied: {ret.actually_copied}; skipped: {ret.skipped}; errors: {ret.errors}"
    )
    return ret


def copy_covers():
    copy_medialibs(
        src_paths=[Path("/data/Music"), Path("/data/MusicOther")],
        dst_root_path=Path("/data/Covers"),
        include_filenames=["cover.jpg"],
        exclude_keywords=[],
        dry_run=False,
        dir_copy_mode=DirCopyMode.PreserveStructure,
        overwrite_existing=False,
        overwrite_newer=False,
    )


def copy_artist_yaml():
    copy_medialibs(
        src_paths=[Path("/data/Music"), Path("/data/MusicOther")],
        dst_root_path=Path("/home/user/Git/moongas-org/moongas/moongas-library"),
        include_filenames=["cover.jpg"],
        exclude_keywords=[],
        dry_run=False,
        dir_copy_mode=DirCopyMode.PreserveStructure,
        overwrite_existing=False,
        overwrite_newer=False,
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Copy media library files maintaining structure or target criteria."
    )

    parser.add_argument(
        "-s",
        "--src-paths",
        type=Path,
        nargs="+",
        default=[Path("/data/Music"), Path("/data/MusicOther")],
        help="One or more source directory paths.",
    )
    parser.add_argument(
        "-d", "--dst-path", type=Path, required=True, help="Destination directory path."
    )
    parser.add_argument(
        "-i",
        "--include-filenames",
        nargs="+",
        default=["cover.jpg"],
        help="Filename glob patterns to include (e.g., cover.jpg artist.yml *.lrc *.txt).",
    )
    parser.add_argument(
        "-e",
        "--exclude-keywords",
        nargs="*",
        default=[],
        help="Keywords to exclude from file paths.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform a trial run without making actual file changes.",
    )
    overwrite_group = parser.add_mutually_exclusive_group()
    overwrite_group.add_argument(
        "--overwrite-existing",
        dest="overwrite_existing",
        action="store_true",
        help="Overwrite existing destination files (excluding newer files) (default: skip existing).",
    )
    overwrite_group.add_argument(
        "--overwrite-newer",
        action="store_true",
        help="Overwrite existing destination files (including newer files) (default: skip newer).",
    )
    parser.add_argument(
        "--dir-copy-mode",
        type=lambda mode: DirCopyMode[mode],
        choices=list(DirCopyMode),
        default=DirCopyMode.PreserveStructure,
        help="Directory structure copy mode.",
    )
    parser.add_argument(
        "--make-track-yml",
        action="store_true",
        help="Instead of copying files, create an empty .yml file per included file (same base name).",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Log the diff of each created/modified file at INFO level instead of DEBUG.",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Logging level (default: INFO).",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="[%(asctime)s] [%(levelname)s] %(name)s - %(message)s",
    )

    copy_medialibs(
        src_paths=args.src_paths,
        dst_root_path=args.dst_path,
        include_filenames=args.include_filenames,
        exclude_keywords=args.exclude_keywords,
        dry_run=args.dry_run,
        overwrite_existing=args.overwrite_existing,
        dir_copy_mode=args.dir_copy_mode,
        overwrite_newer=args.overwrite_newer,
        make_track_yml=args.make_track_yml,
        diff=args.diff,
    )


if __name__ == "__main__":
    main()
