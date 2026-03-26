#!/usr/bin/env bash
set -euo pipefail

# ---- Config (edit these for your repo) ----
OWNER="davetech-dev"
REPO="Pangolin-Updater"
BRANCH="main"
SCRIPT_PATH_IN_REPO="pangolin_updater.py"   # file in repo root
DEST="/usr/local/bin/updater"
# ------------------------------------------

FORCE="0"
if [[ "${1:-}" == "--force" ]]; then
  FORCE="1"
fi

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (or via sudo)."
  echo "Example: curl -fsSL https://raw.githubusercontent.com/$OWNER/$REPO/$BRANCH/install.sh | sudo bash"
  exit 1
fi

TMPDIR="$(mktemp -d)"
cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT

URL="https://raw.githubusercontent.com/$OWNER/$REPO/$BRANCH/$SCRIPT_PATH_IN_REPO"
SRC="$TMPDIR/pangolin_updater"

echo "Downloading: $URL"
curl -fsSL "$URL" -o "$SRC"

# Basic sanity check: must start with python3 shebang
first_line="$(head -n 1 "$SRC" || true)"
if [[ "$first_line" != "#!"*python3* ]]; then
  echo "ERROR: Downloaded script does not start with a python3 shebang."
  echo "Expected first line like: #!/usr/bin/env python3"
  exit 1
fi

# Extract incoming version from __version__ = "X.Y.Z"
INCOMING_VERSION="$(grep -Eo '__version__\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+"' "$SRC" | head -n1 | sed -E 's/.*"([^"]+)".*/\1/')"
if [[ -z "$INCOMING_VERSION" ]]; then
  echo "ERROR: Could not determine incoming version from __version__."
  exit 1
fi

# Get installed version if present
INSTALLED_VERSION=""
if [[ -x "$DEST" ]]; then
  # Expect output like: "pangolin-updater 0.1.0"
  INSTALLED_VERSION="$("$DEST" --version 2>/dev/null | awk '{print $NF}' || true)"
fi

echo "Incoming version : $INCOMING_VERSION"
if [[ -n "$INSTALLED_VERSION" ]]; then
  echo "Installed version: $INSTALLED_VERSION"
else
  echo "Installed version: (not installed)"
fi

# Compare semver: returns 0 if $1 >= $2
version_ge() {
  # sort -V sorts semver-ish correctly for X.Y.Z
  [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

if [[ -n "$INSTALLED_VERSION" && "$FORCE" != "1" ]]; then
  if version_ge "$INSTALLED_VERSION" "$INCOMING_VERSION"; then
    echo "No update performed: installed version is newer or equal."
    echo "Use --force to reinstall anyway."
    exit 0
  fi
fi

install -m 0755 "$SRC" "$DEST"
echo "Installed: $DEST"
echo "Installed version now: $("$DEST" --version || true)"
echo "Run: updater"
