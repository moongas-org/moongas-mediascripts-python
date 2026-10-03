#!/bin/bash
set -euo pipefail
cd $MOONGAS_COLLECTION_ROOTDIR
python -m mediatest mediatest-config.yml \
"$@"
