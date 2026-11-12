#!/bin/bash
set -euo pipefail
HOST=$MOONGAS_REMOTE_SERVER_IP
DEST_USER=root
DEST_ROOT=/var/www/moongas
SVC=moongas-mediatunes-web-vue
SOURCE="$MOONGAS_COLLECTION_ROOTDIR/$SVC/"
DEST=$DEST_USER@$HOST:$DEST_ROOT/$SVC/
echo "Uploading $SOURCE to $DEST"
rsync -ahvP $SOURCE $DEST --delete --exclude '.git/' --exclude '.gitignore' --exclude '__pycache__/' --exclude='.venv*'
echo "Uploaded $SOURCE to $DEST"
ssh $DEST_USER@$HOST "cd /var/www/moongas/moongas-mediatunes-web-vue && deno task build"
echo "Installed/updated moongas-mediatunes-web-vue on $HOST"
DIST=/var/www/moongas/moongas-mediatunes-web-vue/client/dist/
grep -q 'gtag(' $DIST/index.html || sed -i '/<head>/r /var/www/moongas/tag.txt' $DIST/index.html
echo "Inserted Google tag into index.html"
ssh $DEST_USER@$HOST "systemctl restart mediatunes-web"
echo "Restarted mediatunes-web on $HOST"