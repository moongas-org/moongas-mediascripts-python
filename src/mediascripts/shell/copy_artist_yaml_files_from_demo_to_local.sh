#!/bin/bash
set -euo pipefail

# export COPY_MEDIALIB_EXCLUDE_KEYWORDS=$'john smith\njane doe'
mapfile -t exclude_keywords <<< "$COPY_MEDIALIB_EXCLUDE_KEYWORDS"
copy-medialib \
  --src-paths "$MOONGAS_COLLECTION_DEMO/data/Music" \
  --dst-path "$MOONGAS_COLLECTION_LOCAL/data/" \
  --include-filenames artist.yml \
  --dir-copy-mode PreserveStructure \
  --overwrite-existing \
  --exclude-keywords "${exclude_keywords[@]}" \
  --diff \
  "$@"
