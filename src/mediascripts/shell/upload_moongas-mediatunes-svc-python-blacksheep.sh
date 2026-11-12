#!/bin/bash
set -euo pipefail
HOST=$MOONGAS_REMOTE_SERVER_IP
DEST_USER=root
DEST_ROOT=/var/www/moongas
SVC=moongas-mediatunes-svc-python-blacksheep
SOURCE="$MOONGAS_COLLECTION_ROOTDIR/$SVC/"
DEST=$DEST_USER@$HOST:$DEST_ROOT/$SVC/
echo "Uploading $SOURCE to $DEST"
rsync -ahvP $SOURCE $DEST --delete --exclude='*.db' --exclude '.git/' --exclude '.gitignore' --exclude '__pycache__/' --exclude='.venv*'
echo "Uploaded $SOURCE to $DEST"
ssh $DEST_USER@$HOST "cd $DEST_ROOT && uv pip install ./$SVC --upgrade"
echo "Installed/updated $SVC on $HOST"
ssh $DEST_USER@$HOST "systemctl restart $SVC"
echo "Restarted $SVC on $HOST"