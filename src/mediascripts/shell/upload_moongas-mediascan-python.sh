#!/bin/bash
set -euo pipefail
HOST=$MOONGAS_REMOTE_SERVER_IP
DEST_USER=root
DEST_ROOT=/var/www/moongas
SVC=moongas-mediascan-python
SOURCE="$MOONGAS_COLLECTION_ROOTDIR/$SVC/"
DEST=$DEST_USER@$HOST:$DEST_ROOT/$SVC/
echo "Uploading $SOURCE to $DEST"
rsync -ahvP $SOURCE $DEST --delete --exclude '.git/' --exclude '.gitignore' --exclude '__pycache__/' --exclude='.venv*'
echo "Uploaded $SOURCE to $DEST"
