#!/bin/bash
# Updates the active remote Moongas server with the latest media scan database, covers, and restarts services.
set -euo pipefail
./upload-mediascan-db
./upload-covers
./update-lyrics
./restart-remote-services
