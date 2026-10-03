#!/bin/bash
set -euo pipefail
HOST=$MOONGAS_REMOTE_SERVER_IP
DEST_USER=root
DEST_ROOT=/var/www/moongas
SVC=moongas-mediatunes-web-vue
SOURCE="$MOONGAS_COLLECTION_ROOTDIR/$SVC/"
DEST=$DEST_USER@$HOST:$DEST_ROOT/$SVC/
echo "Uploading $SOURCE to $DEST"
# If first run, remove:
# 1) '--exclude 'app/templates/css.html' I added that because I've put my analytics html there
rsync -ahvP $SOURCE $DEST --delete --exclude 'app/templates/css.html' --exclude '.git/' --exclude '.gitignore' --exclude '__pycache__/' --exclude='.venv*'
echo "Uploaded $SOURCE to $DEST"
