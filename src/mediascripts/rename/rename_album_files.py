#!/usr/bin/env python
"""
Usage: rename-album-files [--dir ALBUM_DIR] [--dry-run] [-q | --quiet]

By default, walks every album directory in $MOONGAS_COLLECTION_ROOTDIR/data.
Use --dir to process a single specific album directory instead.
Renames files in accordance with various rules.
Updates references in album YAML files to match the files actually renamed.
Rules:
- Strips any square bracket text like '[Official Music Video]'
- Replaces curved apostrophe with straight apostrophe
- Replaces double quotation marks with single quotes for filesystem compatibility
- Strips stupid little emojis like '🔄'
- etc.
"""

import argparse
import os
import re
import sys
from io import StringIO
from pathlib import Path
from typing import Protocol, cast

from mediascan.types.album.album_path import AlbumPath, AlbumPathBuilder

from mediascripts.track.track_yaml.track_yaml_common import TextWriter, save_yaml_atomically, track_yaml

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
    (re.compile(r'[“”"]'), "'"),
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


class _YamlHandler(Protocol):
    def load(self, stream: str) -> object: ...

    def dump(self, data: object, stream: StringIO) -> object: ...


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


def prepare_rename_tasks(album_path: AlbumPath, *, quiet: bool = False) -> list[tuple[str, str]]:
    rename_tasks: list[tuple[str, str]] = []
    filenames = sorted(f.name for f in album_path.path.iterdir() if f.is_file())
    targets = set(filenames)
    for filename in filenames:
        updated_filename = update_filename(filename, album_path.artist_dirname)
        if updated_filename == filename:
            continue
        if updated_filename in targets:
            if not quiet:
                print(f'Skipping "{filename}": "{updated_filename}" already exists')
            continue
        targets.add(updated_filename)
        rename_tasks.append((filename, updated_filename))
    return rename_tasks


def update_file_references(document: object, renames: dict[str, str]) -> tuple[object, bool]:
    """Update filename/path values without changing directory names or partial matches."""
    if isinstance(document, str):
        filename = re.split(r"[/\\]", document)[-1]
        if filename in renames:
            return document[:-len(filename)] + renames[filename], True
        return document, False
    changed = False
    if isinstance(document, dict):
        mapping = cast(dict[object, object], document)
        for key, value in mapping.items():
            mapping[key], updated = update_file_references(value, renames)
            changed |= updated
    elif isinstance(document, list):
        sequence = cast(list[object], document)
        for index, value in enumerate(sequence):
            sequence[index], updated = update_file_references(value, renames)
            changed |= updated
    return cast(object, document), changed


def perform_rename_tasks(
    album_path: AlbumPath,
    rename_tasks: list[tuple[str, str]],
    dry_run: bool = False,
    *,
    quiet: bool = False,
) -> None:
    yaml_updates: list[tuple[Path, str, str]] = []
    renamed_paths: list[tuple[Path, Path]] = []
    saved_yaml: list[tuple[Path, str]] = []
    if not dry_run:
        renames = dict(rename_tasks)
        # Load and serialize before renaming anything, so invalid YAML cannot break references.
        for path in sorted(album_path.path.iterdir()):
            if not path.is_file() or path.suffix.lower() not in {".yml", ".yaml"}:
                continue
            original_content = path.read_bytes().decode("utf-8")
            yaml = cast(_YamlHandler, track_yaml(preserve_quotes=True))
            document = yaml.load(original_content)
            document, changed = update_file_references(document, renames)
            if changed:
                output = StringIO()
                yaml.dump(document, output)
                updated_path = path.with_name(renames.get(path.name, path.name))
                yaml_updates.append((updated_path, original_content, output.getvalue()))
        try:
            for original, updated in rename_tasks:
                original_path = album_path.path / original
                updated_path = album_path.path / updated
                os.rename(original_path, updated_path)
                renamed_paths.append((original_path, updated_path))
            for path, original_content, updated_content in yaml_updates:
                def write_updated(stream: TextWriter) -> None:
                    stream.write(updated_content)

                save_yaml_atomically(path, write_updated)
                saved_yaml.append((path, original_content))
        except OSError:
            for path, original_content in reversed(saved_yaml):
                def write_original(stream: TextWriter) -> None:
                    stream.write(original_content)

                save_yaml_atomically(path, write_original)
            for original_path, updated_path in reversed(renamed_paths):
                os.rename(updated_path, original_path)
            raise
    for original, updated in rename_tasks:
        if not dry_run or not quiet:
            print(f'Renamed "{original}" -> "{updated}"')


def rename_album_files(album_path_raw: str | Path, dry_run: bool = False, *, quiet: bool = False) -> int:
    if dry_run and not quiet:
        print("----BEGIN DRY RUN----")
    album_path = AlbumPathBuilder.of(album_path_raw)
    if not quiet:
        print("Parsed album path:")
        print(album_path)
    renamed_count = 0
    if not album_path.valid:
        print(f"Album path is invalid, rename operation aborted: {album_path_raw}", file=sys.stderr)
    else:
        if not quiet:
            print("Album path is valid, proceeding with rename operation")
        rename_tasks = prepare_rename_tasks(album_path, quiet=quiet)
        if rename_tasks:
            perform_rename_tasks(album_path, rename_tasks, dry_run, quiet=quiet)
        renamed_count = len(rename_tasks)
        if not quiet:
            print(f"{renamed_count} files renamed")
    if dry_run and not quiet:
        print("----END DRY RUN----")
    return renamed_count


def rename_all_album_files(collection_data_path: Path, dry_run: bool = False, *, quiet: bool = False) -> None:
    if not collection_data_path.is_dir():
        print(f"Collection data directory does not exist: {collection_data_path}", file=sys.stderr)
        return

    def report_walk_error(error: OSError) -> None:
        print(f"Could not scan collection directory: {error}", file=sys.stderr)

    album_dirs = sorted(
        Path(root)
        for root, _, _ in os.walk(collection_data_path, followlinks=True, onerror=report_walk_error)
        if AlbumPathBuilder.of(root).valid
    )
    if not quiet:
        print(f"Found {len(album_dirs)} album directories in {collection_data_path}")
    total_renamed = sum(
        rename_album_files(album_dir, dry_run, quiet=quiet) for album_dir in album_dirs
    )
    if not quiet:
        print(f"{total_renamed} files renamed across {len(album_dirs)} album directories")


def main() -> None:
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
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Report only actual renames and errors; suppress dry-run previews.",
    )

    args = parser.parse_args()
    if args.dir is not None:
        rename_album_files(args.dir, dry_run=args.dry_run, quiet=args.quiet)
    else:
        rename_all_album_files(
            Path(MOONGAS_COLLECTION_ROOTDIR) / "data", dry_run=args.dry_run, quiet=args.quiet
        )


if __name__ == "__main__":
    main()
