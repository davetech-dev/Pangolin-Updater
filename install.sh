#!/usr/bin/env bash
set -euo pipefail

SRC="./pangolin_updater"
DEST="/usr/local/bin/updater"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo $0"
  exit 1
fi

if [[ ! -f "$SRC" ]]; then
  echo "Missing source file: $SRC"
  echo "Run this from the directory that contains pangolin_updater, or edit SRC in this script."
  exit 1
fi

# Ensure the script has a python3 shebang (required for running as a command)
first_line="$(head -n 1 "$SRC" || true)"
if [[ "$first_line" != "#!"*python3* ]]; then
  echo "ERROR: $SRC does not start with a python3 shebang."
  echo "Add this as the first line:"
  echo "#!/usr/bin/env python3"
  exit 1
fi

install -m 0755 "$SRC" "$DEST"

echo "Installed: $DEST"
echo "Test:"
echo "  updater"
