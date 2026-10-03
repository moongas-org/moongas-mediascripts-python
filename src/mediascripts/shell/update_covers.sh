#!/bin/bash
set -euo pipefail

# Step 1: Copy JPG image files from source path to destination path

# $ copy-medialib -h
# usage: copy-medialib [-h] [-s SRC_PATHS [SRC_PATHS ...]] -d DST_PATH [-i INCLUDE_FILENAMES [INCLUDE_FILENAMES ...]]
#                      [-e [EXCLUDE_KEYWORDS ...]] [--dry-run] [--overwrite-existing]
#                      [--dir-copy-mode {DirCopyMode.SingleDirectory,DirCopyMode.PreserveStructure}]

# Copy media library files maintaining structure or target criteria.

# options:
#   -h, --help            show this help message and exit
#   -s, --src-paths SRC_PATHS [SRC_PATHS ...]
#                         One or more source directory paths.
#   -d, --dst-path DST_PATH
#                         Destination directory path.
#   -i, --include-filenames INCLUDE_FILENAMES [INCLUDE_FILENAMES ...]
#                         Filename patterns to include (e.g., cover.jpg artist.yml).
#   -e, --exclude-keywords [EXCLUDE_KEYWORDS ...]
#                         Keywords to exclude from file paths.
#   --dry-run             Perform a trial run without making actual file changes.
#   --overwrite-existing  Overwrite existing destination files (default: skip existing).
#   --dir-copy-mode {DirCopyMode.SingleDirectory,DirCopyMode.PreserveStructure}
#                         Directory structure copy mode.

copy-medialib \
  --src-paths /data/Music /data/MusicOther \
  --dst-path /data/Covers \
  --include-filenames cover.jpg \
  --dir-copy-mode PreserveStructure  \
  "$@"

# Step 2: Convert JPEG (.jpg) image files to WEBP (.webp) in-place

# $ convert-covers -h
# usage: convert-covers [-h] [-s SRC_PATHS [SRC_PATHS ...]] [--src-filename SRC_FILENAME] [--dst-filename DST_FILENAME]
#                       [-e [EXCLUDE_KEYWORDS ...]] [--resolution RESOLUTION] [--quality QUALITY] [--dry-run] [--keep-src-file]
#                       [--overwrite] [--archive-src ARCHIVE_SRC] [--archive-dst ARCHIVE_DST] [--skip-archive]

# Convert medialib cover images in-place and create a tar archive.

# options:
#   -h, --help            show this help message and exit
#   -s, --src-paths SRC_PATHS [SRC_PATHS ...]
#                         Source directory paths for image conversion.
#   --src-filename SRC_FILENAME
#                         Target source filename to look for (default: cover.jpg).
#   --dst-filename DST_FILENAME
#                         Target output filename format (default: cover.webp).
#   -e, --exclude-keywords [EXCLUDE_KEYWORDS ...]
#                         Keywords to exclude matching paths.
#   --resolution RESOLUTION
#                         Target image resolution WxH (default: 1000x1000).
#   --quality QUALITY     Image quality setting 1-100 (default: 80).
#   --dry-run             Perform a dry run without modifying files.
#   --keep-src-file       Do not delete source files after conversion (default: delete original).
#   --overwrite           Overwrite target file if it already exists.
#   --archive-src ARCHIVE_SRC
#                         Source path to archive (default: /data/Covers/).
#   --archive-dst ARCHIVE_DST
#                         Destination tarball file path (default: /data/Covers.tar.gz).
#   --skip-archive        Skip creation of the tar archive after conversion.

convert-covers \
  --src-paths /data/Covers/Music /data/Covers/MusicOther \
  --src-filename cover.jpg \
  --dst-filename cover.webp \
  --resolution 1000x1000 \
  --quality 80 \
  --archive-src /data/Covers/ \
  --archive-dst /data/Covers.tar.gz \
  "$@"
