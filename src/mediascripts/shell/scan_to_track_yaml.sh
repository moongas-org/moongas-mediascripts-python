#!/bin/bash
set -euo pipefail

# Options:
#   No flag               Preserve existing track YAML files; only create new ones if they don't exist.
#   --overwrite-existing  Replace an existing file only if the source is newer. If no source path is supplied or the source is missing, replace it unconditionally.
#   --overwrite-newer     Replace existing output unconditionally, regardless of timestamps.
MGCROOT="$MOONGAS_COLLECTION_ROOTDIR"
go -C "$MGCROOT/moongas-mediascan-go" run \
    ./cmd/scan-to-track-yaml \
    "$MGCROOT/mediascan-config.yml" "$MGCROOT" \
    "$@"
