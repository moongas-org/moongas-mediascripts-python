#!/usr/bin/env bash

set -euo pipefail

# Format the exclude keywords so each newline-separated line is indented
formatted_excludes=""
if [ -n "${COPY_MEDIALIB_EXCLUDE_KEYWORDS:-}" ]; then
    formatted_excludes=$(printf '%s\n' "$COPY_MEDIALIB_EXCLUDE_KEYWORDS" | sed 's/^/    /')
fi

cat << EOF
------------------------------------------------------------------------------
                             Moongas Environment                              
------------------------------------------------------------------------------
  MOONGAS_COLLECTION_ROOTDIR:
    "${MOONGAS_COLLECTION_ROOTDIR:-}"
  MOONGAS_REMOTE_SERVER_IP:
    "${MOONGAS_REMOTE_SERVER_IP:-}"
  COPY_MEDIALIB_EXCLUDE_KEYWORDS:
${formatted_excludes}
------------------------------------------------------------------------------
EOF