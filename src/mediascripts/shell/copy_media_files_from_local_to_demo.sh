#!/bin/bash
set -euo pipefail

# export COPY_MEDIALIB_EXCLUDE_KEYWORDS=$'john smith\njane doe'
mapfile -t exclude_keywords <<< "$COPY_MEDIALIB_EXCLUDE_KEYWORDS"
copy-medialib \
  --src-paths "$MOONGAS_COLLECTION_LOCAL/data/Music" \
  --dst-path "$MOONGAS_COLLECTION_DEMO/data/" \
  --include-filenames "*.mp3" "*.m4a" \
  --dir-copy-mode PreserveStructure \
  --exclude-keywords "${exclude_keywords[@]}" \
  --diff \
  "$@"
