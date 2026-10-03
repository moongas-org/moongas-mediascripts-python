#!/bin/bash

set -euo pipefail

echo "Setting active Moongas media collection to local..."
export MOONGAS_COLLECTION_ROOTDIR=$MOONGAS_COLLECTION_LOCAL
echo "Moongas media collection rootdir is now set to: ${MOONGAS_COLLECTION_ROOTDIR:-}"