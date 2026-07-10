#!/usr/bin/env bash
# Open the SSH port-forward to the cluster. Run this ON THE LAPTOP.
# Keep this terminal open while streaming. Ctrl-C to close the tunnel.
#
#   ./scripts/tunnel.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

run ssh -i "$SSH_KEY" -p "$SSH_PORT" \
  -N -L "$STREAM_PORT:$STREAM_HOST:$STREAM_PORT" \
  "$SSH_USER@$SSH_HOST" \
  "$@"
