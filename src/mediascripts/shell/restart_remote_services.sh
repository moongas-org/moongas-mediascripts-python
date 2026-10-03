#!/bin/bash
set -euo pipefail
HOST=$MOONGAS_REMOTE_SERVER_IP
DEST_USER=root

SVC=mediatunes-svc
ssh -o ConnectTimeout=10 $DEST_USER@$HOST "systemctl stop $SVC && sleep 1 && systemctl start $SVC; echo 'Restarted $SVC'"
echo "Restarted $SVC"

SVC=mediatunes-web
ssh -o ConnectTimeout=10 $DEST_USER@$HOST "systemctl stop $SVC && sleep 1 && systemctl start $SVC; echo 'Restarted $SVC'"
echo "Restarted $SVC"

echo 'Done'
