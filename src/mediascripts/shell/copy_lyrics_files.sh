#!/bin/bash
set -euo pipefail

source "$(dirname -- "$(realpath -- "$0")")/copy_collection.inc"
copy_to_collection lyrics "$@"
