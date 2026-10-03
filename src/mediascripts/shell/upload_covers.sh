#!/bin/bash
set -euo pipefail
rsync -ahvP /data/Covers/ root@$MOONGAS_REMOTE_SERVER_IP:/var/www/html/Covers/ --delete --timeout=10
