copy_to_collection() {
  local kind=$1
  shift
  if (( $# == 0 )); then
    printf 'Usage: %s COLLECTION [copy-medialib options...]\n' "${0##*/}" >&2
    return 2
  fi

  local LC_ALL=C
  local collection=$1
  if [[ ! $collection =~ ^[a-zA-Z0-9_]+$ ]]; then
    printf 'Invalid collection name: %s (use letters, digits, or underscores)\n' "$collection" >&2
    return 2
  fi
  shift

  : "${MOONGAS_COLLECTION_ROOTDIR:?MOONGAS_COLLECTION_ROOTDIR must be set to the source collection}"
  local destination_var="MOONGAS_COLLECTION_${collection^^}"
  local destination
  if ! destination=$(printenv "$destination_var") || [[ -z $destination ]]; then
    printf '%s must be set to the destination collection\n' "$destination_var" >&2
    return 1
  fi

  local -a includes overwrite exclude_keywords
  overwrite=(--overwrite-existing)
  case $kind in
    (album_yaml) includes=(album.yml) ;;
    (all_yaml) includes=('*.yml') ;;
    (artist_yaml) includes=(artist.yml) ;;
    (lyrics) includes=('*.lrc' '*.txt') ;;
    (media) includes=('*.mp3' '*.m4a'); overwrite=() ;;
    (*) printf 'Unknown copy type: %s\n' "$kind" >&2; return 2 ;;
  esac

  exclude_keywords=()
  while IFS= read -r keyword; do
    if [[ -n $keyword ]]; then
      exclude_keywords+=("$keyword")
    fi
  done <<< "$COPY_MEDIALIB_EXCLUDE_KEYWORDS"

  copy-medialib \
    --src-paths "$MOONGAS_COLLECTION_ROOTDIR/data/Music" \
    --dst-path "$destination/data/" \
    --include-filenames "${includes[@]}" \
    --dir-copy-mode PreserveStructure \
    "${overwrite[@]}" \
    --exclude-keywords "${exclude_keywords[@]}" \
    --diff \
    "$@"
}
