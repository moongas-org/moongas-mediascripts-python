# MOONGAS_USER must be supplied by the calling environment
: "${MOONGAS_USER?Error: MOONGAS_USER environment variable is not set.}"

# Load the required variables from MOONGAS_USER's login environment
eval "$(
    sudo -u "$MOONGAS_USER" -i bash -c '
        printf "MOONGAS_COLLECTION_ROOTDIR=%q\n" "$MOONGAS_COLLECTION_ROOTDIR"
        printf "MOONGAS_REMOTE_SERVER_IP=%q\n" "$MOONGAS_REMOTE_SERVER_IP"
        printf "MOONGAS_REMOTE_MEDIA_ROOTDIR=%q\n" "$MOONGAS_REMOTE_MEDIA_ROOTDIR"
    '
)"

# Verify that all required variables were obtained
: "${MOONGAS_COLLECTION_ROOTDIR?Error: MOONGAS_COLLECTION_ROOTDIR is not set.}"
: "${MOONGAS_REMOTE_SERVER_IP?Error: MOONGAS_REMOTE_SERVER_IP is not set.}"
: "${MOONGAS_REMOTE_MEDIA_ROOTDIR?Error: MOONGAS_REMOTE_MEDIA_ROOTDIR is not set.}"

# Updates everything and uploads to the active remote Moongas server
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root." >&2
   exit 1
fi

# [as root] Change to the target directory right now
cd "$MOONGAS_COLLECTION_ROOTDIR"

# [as MOONGAS_USER] Run the following commands as the target Moongas user.
# Using a quoted Here-Doc (<<'EOF') prevents the root shell from expanding variables early,
# and passing it to sudo -u ensures it runs completely in the target user's context.
sudo -i -u "$MOONGAS_USER" MOONGAS_COLLECTION_ROOTDIR="$MOONGAS_COLLECTION_ROOTDIR" MOONGAS_REMOTE_SERVER_IP="$MOONGAS_REMOTE_SERVER_IP" MOONGAS_REMOTE_MEDIA_ROOTDIR="$MOONGAS_REMOTE_MEDIA_ROOTDIR" bash <<'EOF'
  set -euo pipefail
  cd "$MOONGAS_COLLECTION_ROOTDIR"
  ./scan-to-track-yaml
  ./scan-to-db
  ./upload-mediascan-db
  ./update-covers
  ./upload-covers
  ./update-lyrics
  ./update-yaml
  ./restart-remote-services
EOF

# [as root] Restart local services after updating everything locally.
./restart-local-services
