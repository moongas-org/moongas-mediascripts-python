#!/bin/bash
set -euo pipefail

MGCROOT="$MOONGAS_COLLECTION_ROOTDIR"
go -C "$MGCROOT/moongas-mediascan-go" run \
    ./cmd/scan-to-db \
    "$MGCROOT/mediascan-config.yml" \
    "$MGCROOT/mediascan.db" "$MGCROOT" \
    --overwrite-newer \
    "$@"
