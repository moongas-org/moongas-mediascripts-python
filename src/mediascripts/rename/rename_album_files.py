#!/usr/bin/env python
"""
Usage: rename-album-files [--dir ALBUM_DIR] [--dry-run]

By default, walks every album directory in $MOONGAS_COLLECTION_ROOTDIR/data.
Use --dir to process a single specific album directory instead.
Renames files in accordance with various rules.
Rules:
- Strips any square bracket text like '[Official Music Video]'
- Replaces curved apostrophe with straight apostrophe
- Strips stupid little emojis like '🔄'
- etc.
"""

import argparse
import os
import re
from pathlib import Path

from mediascan.utils.path.album_path import AlbumPath, AlbumPathBuilder

MOONGAS_COLLECTION_ROOTDIR = os.environ.get(
    "MOONGAS_COLLECTION_ROOTDIR", "../../moongas-collection-demo"
)

# Matches standard emoji ranges, symbols, and variation selectors e.g. '🔄'
emoji_pattern = re.compile(r"\s*[\U0001F000-\U0001FFFF\u2600-\u27BF\u2300-\u23FF]\s*")

# pattern/substitution, applied to the filename stem only (extension is preserved)
patterns: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\s*\[.*?\]"), ""),
    # E.g. "(Official Music Video)", "(Official HD Video)", "(Lyric Video)", "(Audio)", "(Visualizer)"
    (
        re.compile(
            r"\s*\((?:\s*(?:official|music|video|audio|lyrics?|visuali[sz]er|hd|hq|4k)\b)+\s*\)",
            re.IGNORECASE,
        ),
        "",
    ),
    # E.g. "feat.", "Feat", "FEAT_", "ft.", "Ft", "ft_", "featuring", "Featuring:"
    (re.compile(r"\b(?:featuring|feat|ft)(?:[._:]\s*|\s+)", re.IGNORECASE), "ft "),
    # Dots become underscores, except between digits e.g. disc/track "01.02"
    (re.compile(r"(?<!\d)\.|\.(?!\d)"), "_"),
    (re.compile(r"‐"), "-"),
    (re.compile(r"’"), "'"),
    (re.compile(r"´"), "'"),
    (re.compile(r"“"), '"'),
    (re.compile(r"”"), '"'),
    (re.compile(r"\$"), "_"),
    (re.compile(r"&"), "and"),
    (re.compile(r"＊"), "_"),
    (re.compile(r"\s*\(\s*\)"), ""),
    (re.compile(r"#"), "no "),
    (re.compile(rf"\s*(?:{emoji_pattern.pattern})\s*"), ""),
    (re.compile(r"\s{2,}"), " "),
]

# A " - " style separator; a dash needs whitespace on at least one side so "Jay-Z" stays intact
segment_separator = re.compile(r"\s+[-–—]\s*|\s*[-–—]\s+")


def normalize_artist_name(name: str) -> str:
    return re.sub(r"\s*&\s*", " and ", name).strip().casefold()


def remove_artist_segments(stem: str, artist_dirname: str) -> str:
    """E.g. "03 - Lil Wayne - MegaMan" -> "03 - MegaMan", "U2 - One" -> "One" """
    artist = normalize_artist_name(artist_dirname)
    segments = segment_separator.split(stem)
    kept = [s for s in segments if normalize_artist_name(s) != artist]
    if len(kept) == len(segments) or not kept:
        return stem
    return " - ".join(s.strip() for s in kept)


def update_filename(filename: str, artist_dirname: str) -> str:
    stem, ext = os.path.splitext(filename)
    updated_stem = remove_artist_segments(stem, artist_dirname)
    for pattern, replacement in patterns:
        updated_stem = pattern.sub(replacement, updated_stem)
    updated_stem = updated_stem.strip(" -_")
    return f"{updated_stem or stem}{ext}"


def prepare_rename_tasks(album_path: AlbumPath) -> list[tuple[str, str]]:
    rename_tasks: list[tuple[str, str]] = []
    filenames = sorted(f.name for f in album_path.path.iterdir() if f.is_file())
    targets = set(filenames)
    for filename in filenames:
        updated_filename = update_filename(filename, album_path.artist_dirname)
        if updated_filename == filename:
            continue
        if updated_filename in targets:
            print(f'Skipping "{filename}": "{updated_filename}" already exists')
            continue
        targets.add(updated_filename)
        rename_tasks.append((filename, updated_filename))
    return rename_tasks


def perform_rename_tasks(
    album_path: AlbumPath,
    rename_tasks: list[tuple[str, str]],
    dry_run: bool = False,
) -> None:
    for original, updated in rename_tasks:
        if not dry_run:
            os.rename(
                os.path.join(album_path.path, original),
                os.path.join(album_path.path, updated),
            )
        print(f'Renamed "{original}" -> "{updated}"')


def rename_album_files(album_path_raw: str | Path, dry_run: bool = False) -> int:
    if dry_run:
        print("----BEGIN DRY RUN----")
    album_path = AlbumPathBuilder.of(album_path_raw)
    print("Parsed album path:")
    print(album_path)
    renamed_count = 0
    if not album_path.valid:
        print("Album path is invalid, rename operation aborted")
    else:
        print("Album path is valid, proceeding with rename operation")
        rename_tasks = prepare_rename_tasks(album_path)
        if len(rename_tasks):
            perform_rename_tasks(album_path, rename_tasks, dry_run)
        renamed_count = len(rename_tasks)
        print(f"{renamed_count} files renamed")
    if dry_run:
        print("----END DRY RUN----")
    return renamed_count


def rename_all_album_files(collection_data_path: Path, dry_run: bool = False) -> None:
    if not collection_data_path.is_dir():
        print(f"Collection data directory does not exist: {collection_data_path}")
        return
    album_dirs = sorted(
        Path(root)
        for root, _, _ in os.walk(collection_data_path, followlinks=True)
        if AlbumPathBuilder.of(root).valid
    )
    print(f"Found {len(album_dirs)} album directories in {collection_data_path}")
    total_renamed = sum(
        rename_album_files(album_dir, dry_run) for album_dir in album_dirs
    )
    print(f"{total_renamed} files renamed across {len(album_dirs)} album directories")


def main():
    parser = argparse.ArgumentParser(
        description="Rename album files in all album directories, or a single one with --dir."
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Process a single specific album directory instead of scanning the whole collection.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate actions without making actual file changes",
    )

    args = parser.parse_args()
    if args.dir is not None:
        rename_album_files(args.dir, dry_run=args.dry_run)
    else:
        rename_all_album_files(
            Path(MOONGAS_COLLECTION_ROOTDIR) / "data", dry_run=args.dry_run
        )


if __name__ == "__main__":
    main()
