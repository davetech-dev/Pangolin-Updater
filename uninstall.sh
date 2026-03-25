#!/usr/bin/env bash
set -euo pipefail

DEST="/usr/local/bin/updater"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo $0"
  exit 1
fi

rm -f "$DEST"
echo "Removed: $DEST"
