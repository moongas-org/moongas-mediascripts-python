#!/bin/bash
# Updates everything and uploads to the active remote Moongas server
set -euo pipefail
./scan-to-files-yaml
./scan-to-artists-yaml
./scan-to-track-yaml
./scan-to-db
./upload-mediascan-db
./update-covers
./upload-covers
./update-lyrics
./restart-remote-services
sudo ./restart-local-services
