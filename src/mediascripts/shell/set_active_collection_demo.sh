#!/bin/bash

set -euo pipefail

echo "Setting active Moongas media collection to demo..."
export MOONGAS_COLLECTION_ROOTDIR=$MOONGAS_COLLECTION_DEMO
echo "Moongas media collection rootdir is now set to: ${MOONGAS_COLLECTION_ROOTDIR:-}"
