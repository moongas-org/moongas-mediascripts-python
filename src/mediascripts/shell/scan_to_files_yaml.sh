#!/bin/bash
set -euo pipefail

MGCROOT="$MOONGAS_COLLECTION_ROOTDIR"
go -C "$MGCROOT/moongas-mediascan-go" run \
    ./cmd/scan-to-files-yaml \
    "$MGCROOT/mediascan-config.yml" \
    "$MGCROOT/mediascan-files.yml" "$MGCROOT" \
    --overwrite-newer \
    "$@"
