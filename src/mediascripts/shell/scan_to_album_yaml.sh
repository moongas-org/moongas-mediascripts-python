#!/bin/bash
set -euo pipefail

MGCROOT="$MOONGAS_COLLECTION_ROOTDIR"
go -C "$MGCROOT/moongas-mediascan-go" run \
    ./cmd/scan-to-album-yaml \
    "$MGCROOT/mediascan-config.yml" "$MGCROOT" \
    "$@"
