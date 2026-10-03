#!/bin/bash
# Uploads lyrics (.txt and .lrc) files to the active remote Moongas server,
# mirroring the media library directory structure.
set -euo pipefail
REMOTE_MEDIA_ROOTDIR="${MOONGAS_REMOTE_MEDIA_ROOTDIR:-/data}"
# -k follows the collection's symlinked library dirs (e.g. data/Music)
rsync -ahvPk "$MOONGAS_COLLECTION_ROOTDIR/data/" "root@$MOONGAS_REMOTE_SERVER_IP:$REMOTE_MEDIA_ROOTDIR/" \
  --include='*/' --include='*.txt' --include='*.lrc' --exclude='*' \
  --prune-empty-dirs --timeout=10 \
  "$@"
