#!/bin/bash
set -euo pipefail

# Options:
#   --overwrite-newer    Overwrite existing track YAML files if they are older than the media files
MGCROOT="$MOONGAS_COLLECTION_ROOTDIR"
go -C "$MGCROOT/moongas-mediascan-go" run \
    ./cmd/scan-to-track-yaml \
    "$MGCROOT/mediascan-config.yml" "$MGCROOT" \
    "$@"
