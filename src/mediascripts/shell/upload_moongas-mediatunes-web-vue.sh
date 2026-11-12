#!/bin/bash
set -euo pipefail
DENO_BUILD="false" # Unnessary if building locally beforehand
INSERT_TAG="true"
TAG_FILE=/var/www/moongas/tag.txt
HOST=$MOONGAS_REMOTE_SERVER_IP
DEST_USER=root
DEST_ROOT=/var/www/moongas
SVC=moongas-mediatunes-web-vue
SVC_DIR="/var/www/moongas/$SVC"
SOURCE="$MOONGAS_COLLECTION_ROOTDIR/$SVC/"
DEST=$DEST_USER@$HOST:$DEST_ROOT/$SVC/
echo "Uploading $SOURCE to $DEST"
rsync -ahvP $SOURCE $DEST --delete --exclude '.git/' --exclude '.gitignore' --exclude '__pycache__/' --exclude='.venv*'
echo "Uploaded $SOURCE to $DEST"
if [[ "$DENO_BUILD" == "true" ]]; then
    ssh $DEST_USER@$HOST "cd $SVC_DIR && deno task build"
    echo "Installed/updated $SVC on $HOST"
fi
DIST=$SVC_DIR/client/dist/
if [[ "$INSERT_TAG" == "true" ]]; then
    grep -q 'gtag(' $DIST/index.html || sed -i '/<head>/r $TAG_FILE' $DIST/index.html
    echo "Inserted Google tag into index.html"
fi
ssh $DEST_USER@$HOST "systemctl restart $SVC"
echo "Restarted $SVC on $HOST"