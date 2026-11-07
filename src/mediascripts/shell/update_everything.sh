#!/bin/bash

# If MOONGAS_USER is unset or empty, the script exits immediately with an error
: "${MOONGAS_USER?Error: MOONGAS_USER environment variable is not set.}"

# Updates everything and uploads to the active remote Moongas server
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root." >&2
   exit 1
fi

# [as MOONGAS_USER] Run the following commands as the target Moongas user.
# -i  Start the shell as a login shell for the target user, ensuring their environment is loaded.
sudo -i -u "$MOONGAS_USER" bash -c '
./scan-to-track-yaml
./scan-to-db
./upload-mediascan-db
./update-covers
./upload-covers
./update-lyrics
./restart-remote-services
'

# [as root] Restart local services after updating everything locally.
./restart-local-services

