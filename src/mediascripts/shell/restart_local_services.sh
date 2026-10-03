#!/bin/bash
set -euo pipefail

if (( EUID != 0 )); then
    echo "Error: This script must be run as root or with sudo." >&2
    exit 1
fi

SVC=mediatunes-svc
systemctl stop $SVC && sleep 1 && systemctl start $SVC; echo "Restarted $SVC"
echo "Restarted $SVC"

SVC=mediatunes-web
systemctl stop $SVC && sleep 1 && systemctl start $SVC; echo "Restarted $SVC"
echo "Restarted $SVC"

echo 'Done'
