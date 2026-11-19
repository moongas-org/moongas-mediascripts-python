#!/bin/bash
set -euo pipefail

# export COPY_MEDIALIB_EXCLUDE_KEYWORDS=$'john smith\njane doe'
exclude_keywords=()
while IFS= read -r keyword; do
  if [[ -n "$keyword" ]]; then
    exclude_keywords+=("$keyword")
  fi
done <<< "$COPY_MEDIALIB_EXCLUDE_KEYWORDS"
copy-medialib \
  --src-paths "$MOONGAS_COLLECTION_LOCAL/data/Music" \
  --dst-path "$MOONGAS_COLLECTION_DEMO/data/" \
  --include-filenames "*.yml" \
  --dir-copy-mode PreserveStructure \
  --overwrite-existing \
  --exclude-keywords "${exclude_keywords[@]}" \
  --diff \
  "$@"
