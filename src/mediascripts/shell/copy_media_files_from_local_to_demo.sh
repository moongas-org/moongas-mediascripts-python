#!/bin/bash
set -euo pipefail

copy-medialib \
  --src-paths "$MOONGAS_COLLECTION_LOCAL/data/Music" \
  --dst-path "$MOONGAS_COLLECTION_DEMO/data/" \
  --include-filenames "*.mp3" "*.m4a" \
  --dir-copy-mode PreserveStructure \
  --diff \
  "$@"
