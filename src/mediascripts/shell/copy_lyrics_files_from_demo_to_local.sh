#!/bin/bash
set -euo pipefail

copy-medialib \
  --src-paths "$MOONGAS_COLLECTION_DEMO/data/Music" \
  --dst-path "$MOONGAS_COLLECTION_LOCAL/data/" \
  --include-filenames "*.lrc" "*.txt" \
  --dir-copy-mode PreserveStructure \
  --overwrite-existing \
  --diff \
  "$@"
