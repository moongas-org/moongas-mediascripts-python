#!/bin/bash
set -euo pipefail

if [[ -z "${MOONGAS_COLLECTION_ROOTDIR:-}" ]]; then
    echo "MOONGAS_COLLECTION_ROOTDIR must be set" >&2
    exit 1
fi

data_dir="${MOONGAS_COLLECTION_ROOTDIR}/data"
if [[ ! -d "$data_dir" ]]; then
    printf 'Collection data directory does not exist: %s\n' "$data_dir" >&2
    exit 1
fi

data_dir="$(realpath -- "$data_dir")"
if [[ "$data_dir" == / ]]; then
    echo "Refusing to clean the filesystem root" >&2
    exit 1
fi

printf 'Removing empty directories within: %s\n' "$data_dir"
# Follow collection library links, but preserve the links themselves and the data root.
find -L "$data_dir" -depth -mindepth 1 -type d ! -xtype l -empty -delete -print
