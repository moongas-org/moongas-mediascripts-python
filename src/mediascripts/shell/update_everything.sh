#!/bin/bash

# If MOONGAS_USER is unset or empty, the script exits immediately with an error
: "${MOONGAS_USER?Error: MOONGAS_USER environment variable is not set.}"
: "${MOONGAS_COLLECTION_ROOTDIR?Error: MOONGAS_COLLECTION_ROOTDIR environment variable is not set.}"

# Updates everything and uploads to the active remote Moongas server
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root." >&2
   exit 1
fi

# [as root] Change to the target directory right now
cd "$MOONGAS_COLLECTION_ROOTDIR"

# [as MOONGAS_USER] Run the following commands as the target Moongas user.
# Using a quoted Here-Doc (<<'EOF') prevents the root shell from expanding variables early,
# and passing it to sudo -u ensures it runs completely in the target user's context.
sudo -i -u "$MOONGAS_USER" MOONGAS_COLLECTION_ROOTDIR="$MOONGAS_COLLECTION_ROOTDIR" bash <<'EOF'
  set -euo pipefail
  cd "$MOONGAS_COLLECTION_ROOTDIR"
  ./scan-to-track-yaml
  ./scan-to-db
  ./upload-mediascan-db
  ./update-covers
  ./upload-covers
  ./update-lyrics
  ./restart-remote-services
EOF

# [as root] Restart local services after updating everything locally.
./restart-local-services
