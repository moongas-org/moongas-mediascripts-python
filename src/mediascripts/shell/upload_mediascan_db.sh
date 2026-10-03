#!/bin/bash
set -euo pipefail
rsync -ahvP "$MOONGAS_COLLECTION_ROOTDIR/mediascan.db" root@$MOONGAS_REMOTE_SERVER_IP:/var/www/moongas/mediascan.db --timeout=10
