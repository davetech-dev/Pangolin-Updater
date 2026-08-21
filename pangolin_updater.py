#!/usr/bin/env python3
import os
import re
import sys
import tarfile
import tempfile
import shutil
import subprocess
import threading
import time
import json
import getpass
import hashlib
import hmac
import http.client
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta, timezone

__app_name__ = "pangolin-updater"
__version__ = "0.2.1"

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_CYAN = "\033[36m"


def is_tty():
    return sys.stdout.isatty()


def ui_text(text, color=None, bold=False):
    if not is_tty():
        return text
    parts = []
    if bold:
        parts.append(ANSI_BOLD)
    if color:
        parts.append(color)
    parts.append(text)
    parts.append(ANSI_RESET)
    return "".join(parts)


def term_width(default=80):
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def clear_screen():
    if is_tty():
        # ANSI clear screen + move cursor to home.
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def print_banner():
    cols = term_width()
    if cols < 50:
        print(ui_text(f"{__app_name__} v{__version__}", color=ANSI_CYAN, bold=True))
        return

    width = min(cols, 110)
    line = "=" * width
    print(line)
    print(ui_text(f" {__app_name__} v{__version__} ".center(width), color=ANSI_CYAN, bold=True))
    print(line)


def print_section(title):
    cols = term_width()
    width = min(cols, 110) if cols >= 50 else cols
    print(ui_text(title, bold=True))
    print("-" * max(1, min(width, max(len(title), 24))))


def render_screen(title):
    if not is_tty():
        print(f"[{__app_name__} v{__version__}] {title}")
        return

    clear_screen()
    print_banner()
    print_section(title)

def pause():
    """Holds the screen so output isn't wiped by the next render_screen() clear."""
    input("\nPress Enter to continue...")


# Settings file lives at a fixed location independent of root_dir, since
# root_dir itself is one of the settings it stores.
SETTINGS_FILE = Path("/etc/pangolin-updater/settings.json")

DEFAULT_SETTINGS = {
    "pangolin_edition": "",  # empty = auto-detect from the currently deployed image tag
    "root_dir": "/root",
    "backup_path": "",  # empty = derive from root_dir/backup
    "cloud_backup": {
        "enabled": False,
        "provider": "s3",  # S3-compatible object storage (MinIO, AWS S3, etc.)
        "endpoint": "",
        "bucket": "",
        "access_key": "",
        "secret_key": "",
        "region": "us-east-1",
        "prefix": "",
        "use_path_style": True,
        "verify_ssl": True,
        "encrypt_cloud_backups": False,
        "encryption_passphrase": "",
    },
    "notifications": {
        "enabled": False,
        "type": "",              # "discord" | "slack" | "ntfy" | "generic"
        "webhook_url": "",       # discord / slack / generic
        "ntfy_topic_url": "",    # ntfy's URL is a topic, not a generic webhook
        "notify_on_success": True,
        "notify_on_failure": True,
    },
}

EDITION_OPTIONS = ["Community", "Enterprise"]

# Docker Hub tag prefix fosrl/pangolin uses per edition (GitHub release tags
# themselves are always bare, e.g. "1.21.1" — the prefix is Docker Hub-only).
PANGOLIN_EDITION_PREFIXES = {
    "Enterprise": "ee-",
    "Community": "",
}

def detect_pangolin_tag(tag):
    """
    Splits a deployed fosrl/pangolin tag into (edition, bare_version).
    Returns (None, tag) if the tag uses a variant this tool doesn't model
    yet (e.g. the postgresql- database variant), so callers can fall back
    to treating it as an opaque tag rather than mangling it.
    """
    if not tag:
        return (None, tag)
    if "postgresql-" in tag:
        return (None, tag)
    if tag.startswith("ee-"):
        return ("Enterprise", tag[len("ee-"):])
    return ("Community", tag)

def load_settings():
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
    if not SETTINGS_FILE.exists():
        return merged
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Warning: failed to read settings ({e}); using defaults.")
        return merged
    for key in ("pangolin_edition", "root_dir", "backup_path"):
        if key in data:
            merged[key] = data[key]
    if isinstance(data.get("cloud_backup"), dict):
        merged["cloud_backup"].update(data["cloud_backup"])
    if isinstance(data.get("notifications"), dict):
        merged["notifications"].update(data["notifications"])
    return merged

def save_settings(settings):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    # Settings may contain cloud storage secret keys; keep the file root-only.
    SETTINGS_FILE.chmod(0o600)

SETTINGS = load_settings()

def refresh_paths():
    """Recompute path globals from SETTINGS. Call after any settings change."""
    global ROOT_DIR, COMPOSE_FILE, CONFIG_DIR, BACKUP_DIR
    ROOT_DIR = Path(SETTINGS["root_dir"]).expanduser()
    COMPOSE_FILE = ROOT_DIR / "docker-compose.yml"
    CONFIG_DIR = ROOT_DIR / "config"
    backup_override = SETTINGS.get("backup_path") or ""
    BACKUP_DIR = Path(backup_override).expanduser() if backup_override else ROOT_DIR / "backup"

refresh_paths()
BACKUP_RE = re.compile(r"^pangolin-backup-(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.tar\.gz$")
# Cloud-only: backups encrypted before upload (see encrypt_backup_file) carry
# this suffix. Local backups are never encrypted, so list_backups() never
# needs to match it.
BACKUP_ENCRYPTED_RE = re.compile(r"^pangolin-backup-(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.tar\.gz\.enc$")

@dataclass(frozen=True)
class BackupFile:
    path: Path
    dt: datetime

# Images we manage: key -> (match regex for image line, display name)
IMAGES = {
    "pangolin": {
        "display": "Pangolin",
        "image_repo": "fosrl/pangolin",
        "github_repo": "fosrl/pangolin",
        "release_url": "https://github.com/fosrl/pangolin/releases",
        "upgrade_note": "Recommended by maintainers: upgrade one version at a time, back up each step, and validate before moving to the next version.",
    },
    "gerbil": {
        "display": "Gerbil",
        "image_repo": "fosrl/gerbil",
        "github_repo": "fosrl/gerbil",
        "release_url": "https://github.com/fosrl/gerbil/releases",
    },
    "traefik": {
        "display": "Traefik",
        "image_repo": "traefik",
        "github_repo": "traefik/traefik",
        "release_url": "https://github.com/traefik/traefik/releases",
    },
}

# Traefik major-version lock: per Pangolin maintainer guidance, Traefik should
# stay pinned to v3 until Pangolin's own compatibility guidance says otherwise.
# This is fetched from the repo at update time so the lock can be lifted
# (see TRAEFIK_LOCK_FILE_NAME) without shipping a new updater release.
TRAEFIK_LOCK_URL = "https://raw.githubusercontent.com/davetech-dev/Pangolin-Updater/main/traefik-version-lock.json"
DEFAULT_TRAEFIK_LOCK = {
    "current_traefik_version_tag": "v3",
    "pangolin_last_update_for_traefik_update_to_v4": "",
}

# --- Self-update: check the repo for a newer version, and install it in place ---
UPDATER_SOURCE_URL = "https://raw.githubusercontent.com/davetech-dev/Pangolin-Updater/main/pangolin_updater.py"
UPDATE_INSTALL_DEST = Path("/usr/local/bin/updater")
UPDATE_CHECK_CACHE_FILE = Path("/etc/pangolin-updater/update_check_cache.json")
UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60  # once a day

def _fetch_latest_version(timeout=5):
    req = urllib.request.Request(UPDATER_SOURCE_URL, headers={"User-Agent": f"{__app_name__}/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read(4096).decode("utf-8", "replace")  # __version__ is near the top
    m = re.search(r'__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', text)
    return m.group(1) if m else None

def check_for_update():
    """
    Returns a newer version string if one is available, else None.
    Debounced to once per UPDATE_CHECK_INTERVAL_SECONDS via a small cache
    file; silent and non-blocking on any network failure.
    """
    now = time.time()
    cache = {}
    if UPDATE_CHECK_CACHE_FILE.exists():
        try:
            cache = json.loads(UPDATE_CHECK_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    latest = cache.get("latest_version")
    if now - cache.get("last_checked", 0) >= UPDATE_CHECK_INTERVAL_SECONDS:
        try:
            latest = _fetch_latest_version()
        except Exception:
            pass  # keep whatever was cached; never block startup on network issues
        try:
            UPDATE_CHECK_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            UPDATE_CHECK_CACHE_FILE.write_text(json.dumps({"last_checked": now, "latest_version": latest}), encoding="utf-8")
        except Exception:
            pass

    if latest and compare_versions(latest, __version__) > 0:
        return latest
    return None

def do_self_update():
    require_root()
    force = "--force" in sys.argv
    print(f"Current version: {__version__}")
    print("Fetching latest version from GitHub...")
    try:
        req = urllib.request.Request(UPDATER_SOURCE_URL, headers={"User-Agent": f"{__app_name__}/{__version__}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            new_source = resp.read()
    except Exception as e:
        print(f"ERROR: Failed to download latest version: {e}")
        sys.exit(1)

    text = new_source.decode("utf-8", "replace")
    first_line = text.splitlines()[0] if text else ""
    if not first_line.startswith("#!") or "python3" not in first_line:
        print("ERROR: Downloaded script does not start with a python3 shebang. Aborting.")
        sys.exit(1)

    m = re.search(r'__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', text)
    if not m:
        print("ERROR: Could not determine the downloaded version. Aborting.")
        sys.exit(1)
    latest_version = m.group(1)
    print(f"Latest version:  {latest_version}")

    if not force and compare_versions(latest_version, __version__) <= 0:
        print("Already up to date. Use 'updater --update --force' to reinstall anyway.")
        sys.exit(0)

    tmp_path = UPDATE_INSTALL_DEST.with_suffix(".new")
    tmp_path.write_bytes(new_source)
    tmp_path.chmod(0o755)
    tmp_path.replace(UPDATE_INSTALL_DEST)  # atomic on the same filesystem
    print(f"Updated: {UPDATE_INSTALL_DEST}")
    print(f"Installed version now: {latest_version}")

def handle_cli_flags():
    if len(sys.argv) <= 1:
        return

    if sys.argv[1] in ("--version", "-V"):
        print(f"{__app_name__} {__version__}")
        sys.exit(0)

    if sys.argv[1] in ("--help", "-h"):
        print(f"""Usage:
  updater              Run interactive menu
  updater --version    Show version
  updater --update      Install the latest version from GitHub
  updater --update --force  Reinstall even if already up to date
  updater --backup      Run a backup non-interactively (for cron)
  updater --backup --destination=local|cloud|both  Override the backup destination
  updater --verify-backup  Verify the latest backup is restorable, without touching the live stack
  updater --verify-backup --source=local|cloud  Override which backup to verify
  updater --help       Show help
""")
        sys.exit(0)

    if sys.argv[1] == "--update":
        do_self_update()
        sys.exit(0)

    if sys.argv[1] == "--backup":
        require_root()
        destination_override = None
        for arg in sys.argv[2:]:
            if arg.startswith("--destination="):
                destination_override = arg.split("=", 1)[1]
        if destination_override is not None and destination_override not in ("local", "cloud", "both"):
            print(f"ERROR: Invalid --destination value '{destination_override}'. Must be local, cloud, or both.")
            sys.exit(2)
        ok = do_backup(render=False, interactive=False, destination_override=destination_override)
        sys.exit(0 if ok else 1)

    if sys.argv[1] == "--verify-backup":
        require_root()
        source_override = None
        for arg in sys.argv[2:]:
            if arg.startswith("--source="):
                source_override = arg.split("=", 1)[1]
        if source_override is not None and source_override not in ("local", "cloud"):
            print(f"ERROR: Invalid --source value '{source_override}'. Must be local or cloud.")
            sys.exit(2)
        ok = do_verify_backup(source_override=source_override)
        sys.exit(0 if ok else 1)

    print(f"Unrecognized argument: {sys.argv[1]}")
    print("Run 'updater --help' to see available commands.")
    sys.exit(2)

_stdout_lock = threading.Lock()

def run(cmd, cwd=ROOT_DIR, label=None):
    """
    Run a command, streaming output, while showing a spinner + elapsed time.
    Returns the process return code.
    """
    if label is None:
        label = " ".join(cmd)

    print(f"\n[RUN] {' '.join(cmd)} (cwd={cwd})")

    start = time.time()
    stop_flag = threading.Event()

    def spinner():
        frames = ["|", "/", "-", "\\"]
        i = 0
        while not stop_flag.is_set():
            elapsed = int(time.time() - start)
            msg = f"\r{frames[i % len(frames)]} {label}...  ({elapsed}s elapsed)"
            with _stdout_lock:
                sys.stdout.write(msg)
                sys.stdout.flush()
            time.sleep(0.15)
            i += 1
        with _stdout_lock:
            sys.stdout.write("\r" + " " * 80 + "\r")
            sys.stdout.flush()

    p = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    t = threading.Thread(target=spinner, daemon=True)
    t.start()

    rc = 1
    try:
        for line in p.stdout:
            with _stdout_lock:
                sys.stdout.write("\r" + " " * 80 + "\r")
                sys.stdout.write(line)
                sys.stdout.flush()
        rc = p.wait()
    except BaseException:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        raise
    finally:
        stop_flag.set()
        t.join(timeout=1)

    elapsed = int(time.time() - start)
    print(f"{label} finished in {elapsed}s (exit={rc})")
    return rc

def require_root():
    if os.geteuid() != 0:
        print("This tool must be run as root.")
        sys.exit(1)

def require_paths():
    if not COMPOSE_FILE.exists():
        print(f"Missing {COMPOSE_FILE}")
        sys.exit(1)
    if not CONFIG_DIR.exists() or not CONFIG_DIR.is_dir():
        print(f"Missing {CONFIG_DIR} directory")
        sys.exit(1)

def read_compose_text():
    return COMPOSE_FILE.read_text(encoding="utf-8")

def write_compose_text(text):
    # Make a quick safety copy before writing
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safety_copy = COMPOSE_FILE.with_suffix(f".yml.bak.{ts}")
    shutil.copy2(COMPOSE_FILE, safety_copy)
    COMPOSE_FILE.write_text(text, encoding="utf-8")
    print(f"Updated compose file written. Safety backup: {safety_copy}")

def parse_current_tags(compose_text):
    """
    Returns dict: key -> tag (string after ':') e.g. '1.14.1' or 'v3.6.2'
    """
    tags = {}
    for key, meta in IMAGES.items():
        repo = re.escape(meta["image_repo"])
        # match lines like: image: fosrl/pangolin:1.14.1
        m = re.search(rf'^\s*image:\s*{repo}:(\S+)\s*$', compose_text, re.MULTILINE)
        if not m:
            tags[key] = None
        else:
            tags[key] = m.group(1)
    return tags

def update_image_tag(compose_text, image_repo, new_tag):
    """
    Replaces image tag in compose text for a given repo.
    """
    repo = re.escape(image_repo)
    pattern = rf'^(\s*image:\s*{repo}:)(\S+)(\s*)$'
    def repl(match):
        return f"{match.group(1)}{new_tag}{match.group(3)}"
    new_text, n = re.subn(pattern, repl, compose_text, flags=re.MULTILINE)
    if n == 0:
        raise RuntimeError(f"Could not find image line for {image_repo} in compose file.")
    return new_text

def classify_change(old_tag, new_tag):
    if old_tag is None or new_tag is None:
        return "N/A"
    if old_tag == new_tag:
        return "Unchanged"
    # Best-effort semantic-ish comparison:
    # - strip leading 'v' for traefik style tags
    # - strip known fosrl/pangolin edition prefixes so "ee-1.22.0" compares
    #   correctly against "ee-1.21.1" instead of falling back to lexical compare
    def norm(t):
        if not t:
            return t
        _, bare = detect_pangolin_tag(t)
        t = bare if bare is not None else t
        return t[1:] if t.startswith("v") else t

    o = norm(old_tag)
    n = norm(new_tag)

    # Compare tuple of ints when possible, else fallback to string
    def to_tuple(t):
        parts = t.split(".")
        if all(p.isdigit() for p in parts):
            return tuple(int(p) for p in parts)
        return None

    ot = to_tuple(o) if o else None
    nt = to_tuple(n) if n else None

    if ot is not None and nt is not None:
        if nt > ot:
            return "Upgrade"
        if nt < ot:
            return "Downgrade"
        return "Unchanged"

    # Fallback: lexical compare (not perfect)
    if n > o:
        return "Upgrade"
    if n < o:
        return "Downgrade"
    return "Unchanged"

def parse_version_tuple(tag):
    if not tag:
        return None

    t = tag.strip()
    if t.startswith("v"):
        t = t[1:]

    # Ignore prerelease/build suffixes for basic semver comparison.
    core = t.split("+", 1)[0].split("-", 1)[0]
    parts = core.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)

def compare_versions(tag_a, tag_b):
    a = parse_version_tuple(tag_a)
    b = parse_version_tuple(tag_b)
    if a is None or b is None:
        return 0

    max_len = max(len(a), len(b))
    a_pad = a + (0,) * (max_len - len(a))
    b_pad = b + (0,) * (max_len - len(b))
    if a_pad > b_pad:
        return 1
    if a_pad < b_pad:
        return -1
    return 0

def style_current_tag(tag):
    # Use ANSI bold when writing to a TTY; fallback keeps output readable in logs.
    if sys.stdout.isatty():
        return f"\033[1m{tag}\033[0m"
    return tag

def fetch_traefik_lock(timeout=10):
    """
    Fetches the Traefik major-version lock from the repo. Falls back to the
    hardcoded default (locked to v3) if the fetch fails for any reason.
    """
    lock = dict(DEFAULT_TRAEFIK_LOCK)
    try:
        req = urllib.request.Request(
            TRAEFIK_LOCK_URL,
            headers={"User-Agent": f"{__app_name__}/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for key in DEFAULT_TRAEFIK_LOCK:
            if data.get(key):
                lock[key] = data[key]
    except Exception as e:
        print(f"  Warning: failed to fetch Traefik version lock ({e}); using local default ({lock['current_traefik_version_tag']}).")
    return lock

def fetch_github_release_tags(github_repo, per_page=100, timeout=10):
    url = f"https://api.github.com/repos/{github_repo}/releases?per_page={per_page}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{__app_name__}/{__version__}",
        },
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    tags = []
    for rel in payload:
        tag = rel.get("tag_name")
        if not tag:
            continue
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag_l = tag.lower()
        if any(marker in tag_l for marker in ("-rc", "-ea", "-beta", "-alpha", "-preview", "-dev")):
            continue
        tags.append(tag)
    return tags


# --- Cloud Backup: S3-compatible object storage (MinIO, AWS S3, etc.) ---
#
# Implemented directly against the AWS Signature Version 4 protocol using
# only the standard library, so this tool stays a single dependency-free
# script. Any S3-compatible endpoint (self-hosted MinIO, AWS S3, Backblaze
# B2, Wasabi, DigitalOcean Spaces, ...) works as long as it speaks SigV4.

def cloud_backup_configured():
    cb = SETTINGS["cloud_backup"]
    return bool(cb.get("endpoint") and cb.get("bucket") and cb.get("access_key") and cb.get("secret_key"))

def cloud_backup_ready():
    return bool(SETTINGS["cloud_backup"].get("enabled")) and cloud_backup_configured()

# --- Cloud backup encryption: OpenSSL AES-256-CBC, encrypt-then-MAC ---
#
# openssl enc's own AEAD (GCM) support is version-inconsistent and fiddly to
# script reliably, so encryption uses the long-established CBC+PBKDF2+salt
# pattern, paired with an HMAC-SHA256 integrity/authentication layer built
# from hashlib/hmac (already used by the S3 SigV4 client above) — a standard
# encrypt-then-MAC construction. This lets decrypt fail fast and cleanly on
# a wrong passphrase or corrupted file instead of silently producing garbage.

def _derive_hmac_key(passphrase: str) -> bytes:
    # Separate, fixed-salt derivation purely for the integrity tag — the
    # actual secrecy of the ciphertext comes from openssl's own per-file
    # random salt (-salt), not from this key.
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), b"pangolin-updater-hmac", 100_000)

def encrypt_backup_file(src_path: Path, passphrase: str) -> Path:
    """
    Encrypts src_path via openssl, then appends a 32-byte HMAC-SHA256
    trailer over the ciphertext. Output: src_path with '.enc' appended.
    Does not modify or delete src_path. Raises RuntimeError on failure.
    """
    dest_path = src_path.with_name(src_path.name + ".enc")
    proc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt",
         "-pass", "fd:0", "-in", str(src_path), "-out", str(dest_path)],
        input=passphrase.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"openssl encryption failed: {proc.stderr.decode('utf-8', 'replace').strip()}")

    hmac_key = _derive_hmac_key(passphrase)
    h = hmac.new(hmac_key, digestmod=hashlib.sha256)
    with open(dest_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    with open(dest_path, "ab") as f:
        f.write(h.digest())

    return dest_path

def decrypt_backup_file(src_path: Path, passphrase: str, dest_path: Path) -> None:
    """
    Inverse of encrypt_backup_file(). Streams src_path, splitting off the
    trailing 32-byte HMAC tag and verifying it BEFORE attempting openssl
    decryption, so a wrong passphrase or corrupted file fails cleanly rather
    than producing silent garbage output. Raises RuntimeError on either an
    HMAC mismatch or an openssl failure.
    """
    size = src_path.stat().st_size
    if size < 32:
        raise RuntimeError("encrypted file is too small to contain a valid HMAC tag")
    ciphertext_size = size - 32

    hmac_key = _derive_hmac_key(passphrase)
    h = hmac.new(hmac_key, digestmod=hashlib.sha256)
    tmp_ciphertext = src_path.with_name(src_path.name + ".ciphertext_tmp")
    try:
        with open(src_path, "rb") as fin, open(tmp_ciphertext, "wb") as fout:
            remaining = ciphertext_size
            while remaining > 0:
                chunk = fin.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                h.update(chunk)
                fout.write(chunk)
                remaining -= len(chunk)
            tag = fin.read(32)

        if not hmac.compare_digest(tag, h.digest()):
            raise RuntimeError("integrity check failed: wrong passphrase or corrupted file")

        proc = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000",
             "-pass", "fd:0", "-in", str(tmp_ciphertext), "-out", str(dest_path)],
            input=passphrase.encode("utf-8"),
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"openssl decryption failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    finally:
        try:
            tmp_ciphertext.unlink()
        except Exception:
            pass

def _sha256_file(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def _s3_endpoint_scheme_host(cfg):
    endpoint = (cfg.get("endpoint") or "").strip().rstrip("/")
    if "://" not in endpoint:
        endpoint = "https://" + endpoint
    parsed = urllib.parse.urlparse(endpoint)
    return parsed.scheme or "https", parsed.netloc

def _s3_object_target(cfg, key):
    """Returns (scheme, request_host, canonical_uri) for a given object key."""
    scheme, host = _s3_endpoint_scheme_host(cfg)
    bucket = cfg["bucket"]
    safe_key = "/".join(urllib.parse.quote(seg, safe="") for seg in key.split("/"))
    if cfg.get("use_path_style", True):
        return scheme, host, f"/{bucket}/{safe_key}"
    return scheme, f"{bucket}.{host}", f"/{safe_key}"

def _s3_bucket_target(cfg):
    """Returns (scheme, request_host, canonical_uri) for bucket-level operations (e.g. ListObjects)."""
    scheme, host = _s3_endpoint_scheme_host(cfg)
    bucket = cfg["bucket"]
    if cfg.get("use_path_style", True):
        return scheme, host, f"/{bucket}"
    return scheme, f"{bucket}.{host}", "/"

def _s3_canonical_query(params):
    items = sorted(params.items())
    return "&".join(
        f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in items
    )

def _s3_connect(cfg, scheme, host, timeout):
    hostname, _, port_s = host.partition(":")
    port = int(port_s) if port_s else None
    if scheme == "https":
        ctx = ssl.create_default_context()
        if not cfg.get("verify_ssl", True):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(hostname, port=port, timeout=timeout, context=ctx)
    return http.client.HTTPConnection(hostname, port=port, timeout=timeout)

def _s3_sign(cfg, method, host, canonical_uri, headers, payload_hash, canonical_querystring=""):
    """
    Builds the Authorization header value per AWS Signature Version 4.
    `headers` must already include every header that will be sent and be
    signed (lowercase keys); host/x-amz-date/x-amz-content-sha256 are
    expected to already be present.
    """
    access_key = cfg["access_key"]
    secret_key = cfg["secret_key"]
    region = cfg.get("region") or "us-east-1"
    amzdate = headers["x-amz-date"]
    datestamp = amzdate[:8]

    signed_header_names = sorted(headers.keys())
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in signed_header_names)
    signed_headers = ";".join(signed_header_names)

    canonical_request = "\n".join([
        method, canonical_uri, canonical_querystring, canonical_headers, signed_headers, payload_hash,
    ])
    credential_scope = f"{datestamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amzdate,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    def _hmac(key, msg):
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, "s3")
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

def _s3_amzdate():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def s3_put_bytes(cfg, key, data: bytes, content_type="application/octet-stream", timeout=30):
    scheme, host, canonical_uri = _s3_object_target(cfg, key)
    payload_hash = hashlib.sha256(data).hexdigest()
    headers = {
        "host": host,
        "x-amz-date": _s3_amzdate(),
        "x-amz-content-sha256": payload_hash,
        "content-type": content_type,
        "content-length": str(len(data)),
    }
    headers["authorization"] = _s3_sign(cfg, "PUT", host, canonical_uri, headers, payload_hash)

    conn = _s3_connect(cfg, scheme, host, timeout)
    try:
        conn.request("PUT", canonical_uri, body=data, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status not in (200, 201):
            raise RuntimeError(f"S3 PUT failed ({resp.status}): {body[:300].decode('utf-8', 'replace')}")
    finally:
        conn.close()

def s3_delete(cfg, key, timeout=30):
    scheme, host, canonical_uri = _s3_object_target(cfg, key)
    payload_hash = hashlib.sha256(b"").hexdigest()
    headers = {
        "host": host,
        "x-amz-date": _s3_amzdate(),
        "x-amz-content-sha256": payload_hash,
    }
    headers["authorization"] = _s3_sign(cfg, "DELETE", host, canonical_uri, headers, payload_hash)

    conn = _s3_connect(cfg, scheme, host, timeout)
    try:
        conn.request("DELETE", canonical_uri, headers=headers)
        resp = conn.getresponse()
        resp.read()
        if resp.status not in (200, 202, 204):
            raise RuntimeError(f"S3 DELETE failed ({resp.status})")
    finally:
        conn.close()

def s3_put_file(cfg, local_path: Path, key, content_type="application/gzip", timeout=600):
    """Streams local_path to the object store; never loads the whole file into memory."""
    scheme, host, canonical_uri = _s3_object_target(cfg, key)
    payload_hash = _sha256_file(local_path)
    size = local_path.stat().st_size
    headers = {
        "host": host,
        "x-amz-date": _s3_amzdate(),
        "x-amz-content-sha256": payload_hash,
        "content-type": content_type,
        "content-length": str(size),
    }
    headers["authorization"] = _s3_sign(cfg, "PUT", host, canonical_uri, headers, payload_hash)

    conn = _s3_connect(cfg, scheme, host, timeout)
    try:
        conn.putrequest("PUT", canonical_uri, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status not in (200, 201):
            raise RuntimeError(f"S3 upload failed ({resp.status}): {body[:300].decode('utf-8', 'replace')}")
    finally:
        conn.close()

def s3_test_connection(cfg):
    """Uploads then deletes a tiny marker object to confirm credentials/bucket access."""
    prefix = (cfg.get("prefix") or "").strip("/")
    marker_key = f"{prefix}/.pangolin-updater-connectivity-test".lstrip("/")
    s3_put_bytes(cfg, marker_key, b"pangolin-updater connectivity test\n", content_type="text/plain")
    s3_delete(cfg, marker_key)

def cloud_backup_object_key(backup_name):
    prefix = (SETTINGS["cloud_backup"].get("prefix") or "").strip("/")
    return f"{prefix}/{backup_name}".lstrip("/")

# --- Webhook notifications (Discord / Slack / ntfy / generic) ---

def notifications_configured() -> bool:
    """True if the required fields (type + its URL) are present, regardless of enabled."""
    n = SETTINGS["notifications"]
    if not n.get("type"):
        return False
    if n["type"] == "ntfy":
        return bool(n.get("ntfy_topic_url"))
    return bool(n.get("webhook_url"))

def notifications_ready() -> bool:
    return bool(SETTINGS["notifications"].get("enabled")) and notifications_configured()

def _send_notification_payload(n: dict, success: bool, summary: str, event_label: str = "Backup", timeout: float = 10):
    kind = n["type"]
    status_word = "succeeded" if success else "failed"
    icon = "✅" if success else "❌"
    title = f"{icon} Pangolin {event_label} {status_word}"

    if kind == "discord":
        url = n["webhook_url"]
        body = json.dumps({"content": f"{title}\n{summary}"}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    elif kind == "slack":
        url = n["webhook_url"]
        body = json.dumps({"text": f"{title}\n{summary}"}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    elif kind == "ntfy":
        url = n["ntfy_topic_url"]
        body = summary.encode("utf-8")
        headers = {
            "Title": f"Pangolin {event_label} {status_word.capitalize()}",
            "Priority": "default" if success else "high",
            "Tags": "white_check_mark" if success else "x",
        }
    else:  # generic
        url = n["webhook_url"]
        body = json.dumps({
            "event": event_label.lower(),
            "success": success,
            "summary": summary,
            "host": socket.gethostname(),
            "timestamp": datetime.now().isoformat(),
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}

    # Some providers (Discord's Cloudflare front-end in particular) reject
    # requests with no User-Agent as bot traffic, regardless of payload
    # validity — set one, matching every other outbound request in this file.
    headers["User-Agent"] = f"{__app_name__}/{__version__}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()

def send_notification(success: bool, summary: str, event_label: str = "Backup") -> None:
    """
    Best-effort only: never raises, never affects the caller's own return
    value. Prints a WARNING on failure rather than propagating. event_label
    ("Backup" or "Update") only affects the message text/title — provider,
    URL, and the notify_on_success/failure gates are shared.
    """
    if not notifications_ready():
        return
    n = SETTINGS["notifications"]
    if success and not n.get("notify_on_success", True):
        return
    if not success and not n.get("notify_on_failure", True):
        return
    try:
        _send_notification_payload(n, success, summary, event_label=event_label)
    except Exception as e:
        print(f"WARNING: Failed to send notification: {e}")

def s3_list_objects(cfg, prefix="", timeout=30):
    """Returns a list of {key, size, last_modified} dicts via ListObjectsV2."""
    scheme, host, canonical_uri = _s3_bucket_target(cfg)
    params = {"list-type": "2"}
    if prefix:
        params["prefix"] = prefix
    query = _s3_canonical_query(params)
    payload_hash = hashlib.sha256(b"").hexdigest()
    headers = {
        "host": host,
        "x-amz-date": _s3_amzdate(),
        "x-amz-content-sha256": payload_hash,
    }
    headers["authorization"] = _s3_sign(cfg, "GET", host, canonical_uri, headers, payload_hash, canonical_querystring=query)

    conn = _s3_connect(cfg, scheme, host, timeout)
    try:
        conn.request("GET", f"{canonical_uri}?{query}", headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"S3 LIST failed ({resp.status}): {body[:300].decode('utf-8', 'replace')}")
    finally:
        conn.close()

    import xml.etree.ElementTree as ET
    root = ET.fromstring(body)
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    contents = root.findall("s3:Contents", ns) or root.findall("Contents")
    objects = []
    for c in contents:
        key = c.findtext("s3:Key", default=None, namespaces=ns) or c.findtext("Key", default="")
        size_text = c.findtext("s3:Size", default=None, namespaces=ns) or c.findtext("Size", default="0")
        last_modified = c.findtext("s3:LastModified", default=None, namespaces=ns) or c.findtext("LastModified", default="")
        objects.append({"key": key, "size": int(size_text), "last_modified": last_modified})
    return objects

def s3_get_object_to_file(cfg, key, dest_path: Path, timeout=600):
    """Streams an object to a local file; never loads the whole response into memory."""
    scheme, host, canonical_uri = _s3_object_target(cfg, key)
    payload_hash = hashlib.sha256(b"").hexdigest()
    headers = {
        "host": host,
        "x-amz-date": _s3_amzdate(),
        "x-amz-content-sha256": payload_hash,
    }
    headers["authorization"] = _s3_sign(cfg, "GET", host, canonical_uri, headers, payload_hash)

    conn = _s3_connect(cfg, scheme, host, timeout)
    try:
        conn.request("GET", canonical_uri, headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            body = resp.read()
            raise RuntimeError(f"S3 GET failed ({resp.status}): {body[:300].decode('utf-8', 'replace')}")
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    finally:
        conn.close()

def safe_extract_tar(tar, destination: Path):
    """
    Extract tar safely.
    - Python 3.12+: use filter='data'.
    - Older Python: ensure each member resolves inside destination.
    """
    destination.mkdir(parents=True, exist_ok=True)

    if hasattr(tarfile, "data_filter"):
        tar.extractall(path=destination, filter="data")
        return

    base_dir = destination.resolve()
    safe_members = []
    for member in tar.getmembers():
        # Fallback allowlist: only regular files and directories.
        # This excludes links, device nodes, FIFOs, and other special entries.
        if not (member.isreg() or member.isdir()):
            print(f"Skipping unsupported tar member type: {member.name}")
            continue

        dest_path = (base_dir / member.name).resolve()
        try:
            dest_path.relative_to(base_dir)
        except ValueError:
            print(f"Skipping potentially unsafe path in tar archive: {member.name}")
            continue
        safe_members.append(member)

    for member in safe_members:
        tar.extract(member, path=base_dir)

def select_release_tag(meta, current_tag, major_version_lock=None, annotate_from_tag=None, pangolin_edition_setting=None):
    display = meta["display"]
    github_repo = meta.get("github_repo")
    release_url = meta.get("release_url")
    upgrade_note = meta.get("upgrade_note")

    # Edition-aware handling (Pangolin only): GitHub release tags are always
    # bare (e.g. "1.21.1"), but Docker Hub prefixes Enterprise Edition images
    # with "ee-". pangolin_edition_setting is None for gerbil/traefik.
    use_edition_logic = False
    edition_prefix = ""
    compare_basis = current_tag
    if pangolin_edition_setting is not None:
        detected_edition, bare_current = detect_pangolin_tag(current_tag)
        if detected_edition is None:
            print(f"  Warning: current tag '{current_tag}' uses a variant (e.g. Postgres) this tool doesn't manage edition-wise yet; select/enter tags manually.")
        else:
            target_edition = pangolin_edition_setting if pangolin_edition_setting in PANGOLIN_EDITION_PREFIXES else detected_edition
            edition_prefix = PANGOLIN_EDITION_PREFIXES[target_edition]
            compare_basis = bare_current
            use_edition_logic = True
            if target_edition != detected_edition:
                print(f"  NOTE: Settings > Pangolin Edition is '{target_edition}', but the running image is '{detected_edition}'. Picking an update below will switch editions ({detected_edition} -> {target_edition}).")

    def full_tag(bare):
        return f"{edition_prefix}{bare}" if use_edition_logic else bare

    def annotation_for(tag):
        if not annotate_from_tag:
            return ""
        threshold = parse_version_tuple(annotate_from_tag)
        tt = parse_version_tuple(tag)
        if threshold is None or tt is None:
            return ""
        max_len = max(len(threshold), len(tt))
        t_pad = threshold + (0,) * (max_len - len(threshold))
        v_pad = tt + (0,) * (max_len - len(tt))
        if v_pad >= t_pad:
            return "  [!] Requires updating Traefik to v4"
        return ""

    if current_tag is None:
        val = input(f"Enter {display} version tag to pin (current not detected) [leave blank to keep]: ").strip()
        return val if val else current_tag

    print(f"\n{display} versions:")
    if release_url:
        print(f"  Releases: {release_url}")
    if upgrade_note:
        print(f"  NOTE: {upgrade_note}")
    if major_version_lock:
        print(f"  NOTE: Locked to {major_version_lock}.x per Pangolin maintainer guidance. Only that major version is offered below.")

    if not github_repo:
        print("  Release source not configured.")
        print(f"  [0] {style_current_tag(current_tag)} (Current)")
        val = input(f"Choose number [default: 0], or type tag manually: ").strip()
        if val in ("", "0"):
            return current_tag
        return val

    try:
        release_tags = fetch_github_release_tags(github_repo)
    except urllib.error.URLError as e:
        print(f"  Failed to fetch releases: {e}")
        print(f"  [0] {style_current_tag(current_tag)} (Current)")
        val = input(f"Choose number [default: 0], or type tag manually: ").strip()
        if val in ("", "0"):
            return current_tag
        return val
    except Exception as e:
        print(f"  Failed to parse releases: {e}")
        print(f"  [0] {style_current_tag(current_tag)} (Current)")
        val = input(f"Choose number [default: 0], or type tag manually: ").strip()
        if val in ("", "0"):
            return current_tag
        return val

    # Keep unique semver-like tags only.
    unique_tags = []
    seen = set()
    for tag in release_tags:
        if tag in seen:
            continue
        if parse_version_tuple(tag) is None:
            continue
        seen.add(tag)
        unique_tags.append(tag)

    if major_version_lock:
        locked_major = parse_version_tuple(major_version_lock)
        if locked_major is not None:
            unique_tags = [
                t for t in unique_tags
                if parse_version_tuple(t) is not None and parse_version_tuple(t)[0] == locked_major[0]
            ]

    # If current tag is non-semver-like (e.g. "latest"), still show stable
    # release options so users can pick a concrete version from the menu.
    current_parsed = parse_version_tuple(compare_basis)
    if current_parsed is None:
        upgrades = list(unique_tags)
        downgrades = []
    else:
        upgrades = [t for t in unique_tags if compare_versions(t, compare_basis) > 0]
        downgrades = [t for t in unique_tags if compare_versions(t, compare_basis) < 0]

    # Sort upgrades newest first.
    upgrades.sort(key=lambda t: parse_version_tuple(t), reverse=True)

    # Keep only one downgrade: nearest lower version.
    one_downgrade = None
    if downgrades:
        one_downgrade = max(downgrades, key=lambda t: parse_version_tuple(t))

    option_map = {}
    idx = 1
    for tag in upgrades:
        option_map[idx] = full_tag(tag)
        print(f"  [{idx}] {full_tag(tag)} (Upgrade){annotation_for(tag)}")
        idx += 1

    current_idx = idx
    option_map[current_idx] = current_tag
    print(f"  [{current_idx}] {style_current_tag(current_tag)} (Current){annotation_for(compare_basis)}")
    idx += 1

    if one_downgrade is not None:
        option_map[idx] = full_tag(one_downgrade)
        print(f"  [{idx}] {full_tag(one_downgrade)} (Downgrade){annotation_for(one_downgrade)}")

    if len(upgrades) == 0:
        print("  No stable upgrades found; keeping current is recommended.")

    val = input(f"Choose version number [default: {current_idx}], or type tag manually: ").strip()
    if val == "":
        return current_tag
    if val.isdigit():
        pick = int(val)
        if pick in option_map:
            return option_map[pick]
        print("Invalid number; keeping current.")
        return current_tag
    return val

# Pangolin maintains its own rolling SQLite snapshots under config/db/backups/;
# only the live db.sqlite is needed to restore current state, so the snapshot
# history (and any stray .bak.* copies) is excluded to keep archives small.
BACKUP_EXCLUDE_DIRS = {"config/db/backups"}
BACKUP_EXCLUDE_FILE_RE = re.compile(r"\.bak(\.|$)")

def _backup_tar_filter(tarinfo):
    name = tarinfo.name
    if name in BACKUP_EXCLUDE_DIRS or any(
        name.startswith(f"{d}/") for d in BACKUP_EXCLUDE_DIRS
    ):
        return None
    if BACKUP_EXCLUDE_FILE_RE.search(Path(name).name):
        return None
    return tarinfo

def verify_backup_integrity(backup_path: Path) -> tuple[bool, str]:
    """
    Forces a full read of the gzip+tar stream (tarfile raises on truncation
    or corruption) without extracting any payload to disk, then checks the
    two mandatory top-level entries are present. Returns (ok, message).
    """
    try:
        with tarfile.open(backup_path, "r:gz") as tar:
            names = tar.getnames()
    except Exception as e:
        return False, f"could not read archive: {e}"

    if "docker-compose.yml" not in names:
        return False, "archive is missing docker-compose.yml"
    if not any(n == "config" or n.startswith("config/") for n in names):
        return False, "archive is missing the config/ directory"
    return True, "ok"

def _default_backup_destination():
    """Used when do_backup() runs non-interactively (e.g. `updater --backup`)
    with no explicit destination_override: prefer cloud+local if cloud is
    configured and enabled, otherwise fall back to local only."""
    return "both" if cloud_backup_ready() else "local"

def do_backup(render: bool = True, interactive: bool = True, destination_override: str | None = None) -> bool:
    """
    Runs the full backup pipeline. Every prompt is gated behind `interactive`,
    so this same function backs both the menu (interactive=True) and
    `updater --backup` (interactive=False, no input() calls at all).
    Returns True on success, False if any stage failed — used for the CLI
    flag's exit code and (later) notification content.
    """
    if render:
        render_screen("Backup")
    require_paths()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    result = True
    summary_lines = []

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_name = f"pangolin-backup-{ts}.tar.gz"
    backup_path = BACKUP_DIR / backup_name

    with tarfile.open(backup_path, "w:gz", compresslevel=6) as tar:
        tar.add(str(COMPOSE_FILE), arcname="docker-compose.yml")
        tar.add(str(CONFIG_DIR), arcname="config", filter=_backup_tar_filter)

    print(f"\nBackup created: {backup_path}")

    ok, msg = verify_backup_integrity(backup_path)
    if not ok:
        print(f"ERROR: Backup integrity check failed: {msg}")
        summary_lines.append(f"integrity_check=failed ({msg})")
        result = False
        corrupt_path = backup_path.with_name(backup_path.name + ".corrupt")
        try:
            backup_path.rename(corrupt_path)
            print(f"Renamed for diagnosis: {corrupt_path}")
        except Exception as e:
            print(f"WARNING: Failed to rename corrupt backup: {e}")
    else:
        print("Integrity check passed.")
        summary_lines.append("integrity_check=ok")

    if result:
        if destination_override is not None:
            destination = destination_override
        elif interactive:
            destination = prompt_backup_destination()
        else:
            destination = _default_backup_destination()
        summary_lines.append(f"destination={destination}")

        if destination in ("cloud", "both"):
            cb = SETTINGS["cloud_backup"]
            upload_path = backup_path
            upload_key_suffix = ""
            encrypted_tmp = None
            encryption_ok = True

            if cb.get("encrypt_cloud_backups"):
                passphrase = cb.get("encryption_passphrase") or ""
                if not passphrase:
                    print("WARNING: Cloud encryption is enabled but no passphrase is configured. Skipping upload.")
                    summary_lines.append("cloud_upload=skipped (no encryption passphrase)")
                    result = False
                    encryption_ok = False
                else:
                    try:
                        print("\nEncrypting backup for cloud upload...")
                        encrypted_tmp = encrypt_backup_file(backup_path, passphrase)
                        upload_path = encrypted_tmp
                        upload_key_suffix = ".enc"
                        print("Encryption complete.")
                    except Exception as e:
                        print(f"ERROR: Encryption failed: {e}")
                        summary_lines.append(f"encryption=failed ({e})")
                        result = False
                        encryption_ok = False

            key = cloud_backup_object_key(backup_name) + upload_key_suffix
            cloud_upload_ok = False
            if encryption_ok:
                print(f"\nUploading to cloud storage (s3://{cb['bucket']}/{key})...")
                try:
                    s3_put_file(cb, upload_path, key)
                    print("Cloud upload complete.")
                    summary_lines.append("cloud_upload=ok")
                    cloud_upload_ok = True
                    if destination == "cloud":
                        try:
                            backup_path.unlink()
                            print("Local copy removed (Cloud only).")
                        except Exception as e:
                            print(f"WARNING: Failed to remove local copy: {e}")
                except Exception as e:
                    print(f"WARNING: Cloud upload failed: {e}")
                    summary_lines.append(f"cloud_upload=failed ({e})")
                    result = False
                    if destination == "cloud":
                        print("Keeping local copy since the cloud upload failed.")

            if encrypted_tmp is not None and encrypted_tmp.exists():
                try:
                    encrypted_tmp.unlink()
                except Exception as e:
                    print(f"WARNING: Failed to remove temporary encrypted file: {e}")

            if cloud_upload_ok:
                prefix = (cb.get("prefix") or "").strip("/")
                print("\nApplying cloud backup retention policy...")
                kept_c, deleted_c = apply_cloud_backup_retention(cb, prefix=prefix)
                print(f"Cloud retention done. Kept: {len(kept_c)}  Deleted: {len(deleted_c)}")
                summary_lines.append(f"cloud_retention kept={len(kept_c)} deleted={len(deleted_c)}")

        print("\nApplying backup retention policy in /root/backup ...")
        kept, deleted = apply_backup_retention(BACKUP_DIR)
        print(f"Retention done. Kept: {len(kept)}  Deleted: {len(deleted)}")
        summary_lines.append(f"local_retention kept={len(kept)} deleted={len(deleted)}")

    if interactive:
        cleanup_baks = input("\nCleanup all docker-compose .bak files in /root now? (Y/N) [default: N]: ").strip().lower()
        if cleanup_baks in ("y", "yes"):
            removed = cleanup_compose_bak_files()
            print(f"Removed compose backups: {removed}")

    summary = f"host={socket.gethostname()} " + " ".join(summary_lines)
    send_notification(result, summary, event_label="Backup")

    return result

def prompt_backup_destination():
    ready = cloud_backup_ready()
    print("\nWhere do you want to store this backup?")
    print("  [1] Local only (default)")
    if ready:
        print("  [2] Cloud only")
        print("  [3] Both Local and Cloud")
    else:
        print("  [2] Cloud only            (not available — set up Cloud Backup in Settings first)")
        print("  [3] Both Local and Cloud  (not available — set up Cloud Backup in Settings first)")
    val = input("Choose [default: 1]: ").strip()
    if val in ("2", "3") and not ready:
        print("Cloud Backup isn't configured yet. Go to Settings > Cloud Backup to set it up. Defaulting to Local only.")
        return "local"
    if val == "2":
        return "cloud"
    if val == "3":
        return "both"
    if val not in ("", "1"):
        print("Invalid choice. Defaulting to Local only.")
    return "local"

def cleanup_compose_bak_files() -> int:
    pattern = "docker-compose.yml.bak.*"
    removed = 0
    for p in ROOT_DIR.glob(pattern):
        if p.is_file():
            try:
                p.unlink()
                removed += 1
            except Exception as e:
                print(f"Warning: failed to delete {p}: {e}")
    return removed

def list_backups(backup_dir: Path) -> list[BackupFile]:
    items: list[BackupFile] = []
    if not backup_dir.exists():
        return items

    for p in backup_dir.iterdir():
        if not p.is_file():
            continue
        m = BACKUP_RE.match(p.name)
        if not m:
            continue
        date_part = m.group(1)          # YYYY-MM-DD
        time_part = m.group(2)          # HH-MM-SS
        dt = datetime.strptime(f"{date_part}_{time_part}", "%Y-%m-%d_%H-%M-%S")
        items.append(BackupFile(path=p, dt=dt))

    items.sort(key=lambda b: b.dt)  # oldest -> newest
    return items

def _select_retained(items, now: datetime | None = None) -> set:
    """
    Pure day/3-day/2-week/month keep-decision, no I/O. `items` is a list of
    (identifier, dt) pairs — identifier can be anything hashable (a local
    Path, or an S3 object key string). Returns the set of identifiers to
    keep; shared by local (apply_backup_retention) and cloud
    (apply_cloud_backup_retention) retention so the policy only lives once.
    """
    if now is None:
        now = datetime.now()

    if not items:
        return set()

    # Group by day/week/month
    by_day = defaultdict(list)
    by_week = defaultdict(list)   # (iso_year, iso_week)
    by_month = defaultdict(list)  # (year, month)

    for identifier, dt in items:
        day_key = dt.date()
        iso_year, iso_week, _ = dt.isocalendar()
        week_key = (iso_year, iso_week)
        month_key = (dt.year, dt.month)

        by_day[day_key].append((identifier, dt))
        by_week[week_key].append((identifier, dt))
        by_month[month_key].append((identifier, dt))

    # Helper: latest in group
    def latest(group):
        return max(group, key=lambda x: x[1])[0]

    keep = set()

    today = now.date()

    # 1) Keep ALL from today
    for identifier, dt in by_day.get(today, []):
        keep.add(identifier)

    # 2) Keep latest from each of previous 3 days
    for delta in (1, 2, 3):
        d = (now.date() - timedelta(days=delta))
        if d in by_day:
            keep.add(latest(by_day[d]))

    # 3) Keep latest from previous 2 weeks (excluding current week)
    current_iso_year, current_iso_week, _ = now.isocalendar()

    # Compute the ISO week keys for "previous 1 week" and "previous 2 weeks"
    # We’ll do it by stepping back 7 and 14 days and taking their ISO week.
    prev_week_1 = (now - timedelta(days=7)).isocalendar()
    prev_week_2 = (now - timedelta(days=14)).isocalendar()
    prev_week_keys = {
        (prev_week_1[0], prev_week_1[1]),
        (prev_week_2[0], prev_week_2[1]),
    }
    # Remove current week if it collided (edge cases)
    prev_week_keys.discard((current_iso_year, current_iso_week))

    for wk in prev_week_keys:
        if wk in by_week:
            keep.add(latest(by_week[wk]))

    # 4) For older backups (anything not already covered), keep latest per month
    # “Older” here means: not today, not in last 3 days, and not in the two previous weeks.
    covered_days = {today, today - timedelta(days=1), today - timedelta(days=2), today - timedelta(days=3)}
    covered_weeks = prev_week_keys | {(current_iso_year, current_iso_week)}

    for month_key, group in by_month.items():
        # Determine if this month group contains any backup outside the covered windows.
        # If the month has *only* covered backups, monthly retention isn’t needed.
        has_older = False
        for identifier, dt in group:
            d = dt.date()
            iso_year, iso_week, _ = dt.isocalendar()
            if (d not in covered_days) and ((iso_year, iso_week) not in covered_weeks):
                has_older = True
                break

        if has_older:
            keep.add(latest(group))

    return keep

def apply_backup_retention(backup_dir: Path, now: datetime | None = None, dry_run: bool = False) -> tuple[list[Path], list[Path]]:
    """
    Returns (kept_paths, deleted_paths)
    """
    backups = list_backups(backup_dir)
    if not backups:
        return ([], [])

    keep = _select_retained([(b.path, b.dt) for b in backups], now=now)
    kept = sorted(keep)
    deleted = [b.path for b in backups if b.path not in keep]

    if not dry_run:
        for p in deleted:
            try:
                p.unlink()
            except Exception as e:
                print(f"Warning: failed to delete backup {p}: {e}")

    return (kept, deleted)

def list_cloud_backups(cfg: dict, prefix: str = "") -> list[tuple[str, datetime]]:
    """
    Mirrors list_backups(): lists cloud objects under prefix, filters by
    BACKUP_ENCRYPTED_RE (tried first) or plain BACKUP_RE against the
    basename, returns [(full_object_key, dt), ...]. Matching both patterns
    means turning encryption on/off doesn't orphan already-uploaded objects
    from retention/restore.
    """
    objects = s3_list_objects(cfg, prefix=prefix)
    items = []
    for obj in objects:
        name = obj["key"].rsplit("/", 1)[-1]
        m = BACKUP_ENCRYPTED_RE.match(name) or BACKUP_RE.match(name)
        if not m:
            continue
        dt = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y-%m-%d_%H-%M-%S")
        items.append((obj["key"], dt))
    return items

def apply_cloud_backup_retention(cfg: dict, prefix: str = "", now: datetime | None = None, dry_run: bool = False) -> tuple[list[str], list[str]]:
    """
    Returns (kept_keys, deleted_keys). Same day/week/month policy as
    apply_backup_retention (via the shared _select_retained), applied to
    cloud object keys instead of local paths, deleting via s3_delete().
    """
    backups = list_cloud_backups(cfg, prefix=prefix)
    if not backups:
        return ([], [])

    keep = _select_retained(backups, now=now)
    kept = sorted(keep)
    deleted = [key for key, _dt in backups if key not in keep]

    if not dry_run:
        for key in deleted:
            try:
                s3_delete(cfg, key)
            except Exception as e:
                print(f"Warning: failed to delete cloud backup {key}: {e}")

    return (kept, deleted)


def do_update():
    render_screen("Update")
    require_paths()

    backup_ans = input("Take a backup before updating? (Y/N) [default: Y]: ").strip().lower()
    if backup_ans in ("", "y", "yes"):
        do_backup(render=False)

    compose_text = read_compose_text()
    current = parse_current_tags(compose_text)

    if any(v is None for v in current.values()):
        print("Warning: Could not detect one or more image tags from docker-compose.yml.")
        print("Detected tags:", current)

    print("\nChecking GitHub releases and preparing version choices...")
    traefik_lock = fetch_traefik_lock()

    selections = {}
    for key, meta in IMAGES.items():
        old = current.get(key)
        major_version_lock = traefik_lock["current_traefik_version_tag"] if key == "traefik" else None
        annotate_from_tag = traefik_lock["pangolin_last_update_for_traefik_update_to_v4"] if key == "pangolin" else None
        pangolin_edition_setting = SETTINGS["pangolin_edition"] if key == "pangolin" else None
        selections[key] = select_release_tag(
            meta, old,
            major_version_lock=major_version_lock,
            annotate_from_tag=annotate_from_tag,
            pangolin_edition_setting=pangolin_edition_setting,
        )

    print("\nPlanned changes:")
    any_changes = False
    for key, meta in IMAGES.items():
        old = current.get(key)
        new = selections.get(key)
        change = classify_change(old, new)
        print(f"- {meta['display']}: {old} -> {new}  ({change})")
        if old != new:
            any_changes = True

    if not any_changes:
        print("\nNo version changes selected. Nothing to do.")
        return

    ans = input("\nProceed? (Y/N) [default: N]: ").strip().lower()
    if ans not in ("y", "yes"):
        print("Cancelled.")
        return

    # Apply updates
    new_text = compose_text
    changed_services = []
    for key, meta in IMAGES.items():
        old = current.get(key)
        new = selections.get(key)
        if old != new and new is not None:
            try:
                new_text = update_image_tag(new_text, meta["image_repo"], new)
                changed_services.append(key)
            except RuntimeError as e:
                print(f"Warning: {e} — skipping {meta['display']}.")

    if not changed_services:
        print("\nNo updates could be applied to docker-compose.yml. Skipping restart.")
        send_notification(False, f"host={socket.gethostname()} no updates could be applied to docker-compose.yml", event_label="Update")
        return

    write_compose_text(new_text)
    changes_summary = ", ".join(f"{key}={selections[key]}" for key in changed_services)

    # Only pull + recreate the services that actually changed, leaving the
    # rest of the stack running undisturbed.
    print(f"\nRestarting only: {', '.join(changed_services)}")
    rc = run(["docker", "compose", "pull"] + changed_services, cwd=ROOT_DIR)
    if rc != 0:
        print("docker compose pull failed; aborting.")
        send_notification(False, f"host={socket.gethostname()} docker compose pull failed for: {changes_summary}", event_label="Update")
        sys.exit(rc)

    rc = run(["docker", "compose", "up", "-d"] + changed_services, cwd=ROOT_DIR)
    if rc != 0:
        print("docker compose up -d failed.")
        send_notification(False, f"host={socket.gethostname()} docker compose up -d failed for: {changes_summary}", event_label="Update")
        sys.exit(rc)

    print("\nUpdate complete.")

    # Cleanup: remove unused images
    cleanup = input("\nCleanup unused Docker images now? (Y/N) [default: Y]: ").strip().lower()
    if cleanup in ("", "y", "yes"):
        # This removes only *dangling* images. If your old images are still referenced (common),
        # it may report nothing to prune. We follow up with an "unused" prune.
        rc = run(["docker", "image", "prune", "-f"], cwd=ROOT_DIR)
        if rc != 0:
            print("Warning: docker image prune failed (continuing).")

        # This removes *unused* images (not just dangling) which is what you expect after upgrades.
        rc = run(["docker", "image", "prune", "-a", "-f"], cwd=ROOT_DIR)
        if rc != 0:
            print("Warning: docker image prune -a failed (continuing).")
        else:
            print("Unused images removed.")

    send_notification(True, f"host={socket.gethostname()} updated: {changes_summary}", event_label="Update")

def prompt_restore_source():
    ready = cloud_backup_ready()
    print("\nRestore from:")
    print("  [1] Local (default)")
    if ready:
        print("  [2] Cloud")
    else:
        print("  [2] Cloud   (not available — set up Cloud Backup in Settings first)")
    val = input("Choose [default: 1]: ").strip()
    if val == "2" and not ready:
        print("Cloud Backup isn't configured yet. Go to Settings > Cloud Backup to set it up. Defaulting to Local.")
        return "local"
    if val == "2":
        return "cloud"
    return "local"

def _download_and_decrypt_cloud_backup(cb: dict, key: str, name: str, dest_dir: Path) -> tuple[Path, str]:
    """
    Downloads a cloud backup object into dest_dir, decrypting it first if
    it's a .enc object (using the configured passphrase, prompting if none
    is set). Returns (plaintext_path, plaintext_name). Raises on failure —
    caller is responsible for cleaning up any partial download. Shared by
    do_restore()'s cloud branch and do_verify_backup().
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    downloaded = dest_dir / name
    s3_get_object_to_file(cb, key, downloaded)
    print("Download complete.")

    if not name.endswith(".enc"):
        return downloaded, name

    passphrase = cb.get("encryption_passphrase") or ""
    if not passphrase:
        passphrase = getpass.getpass("Enter the encryption passphrase for this backup: ")
    plain_name = name[:-4]  # strip '.enc'
    decrypted_path = dest_dir / plain_name
    print("Decrypting...")
    decrypt_backup_file(downloaded, passphrase, decrypted_path)
    downloaded.unlink()  # remove the encrypted intermediate
    print("Decryption complete.")
    return decrypted_path, plain_name

def do_restore():
    render_screen("Restore")
    source = prompt_restore_source()

    downloaded_tmp = None
    if source == "cloud":
        cb = SETTINGS["cloud_backup"]
        prefix = (cb.get("prefix") or "").strip("/")
        print("\nFetching backup list from cloud storage...")
        try:
            objects = s3_list_objects(cb, prefix=prefix)
        except Exception as e:
            print(f"Failed to list cloud backups: {e}")
            send_notification(False, f"host={socket.gethostname()} failed to list cloud backups: {e}", event_label="Restore")
            return

        candidates = []
        for obj in objects:
            name = obj["key"].rsplit("/", 1)[-1]
            if BACKUP_ENCRYPTED_RE.match(name) or BACKUP_RE.match(name):
                candidates.append((obj, name))
        if not candidates:
            print(f"\nNo backups found in cloud storage (bucket: {cb['bucket']}, prefix: {prefix or '(root)'}).")
            return
        candidates.sort(key=lambda c: c[1])  # filename timestamp sorts chronologically

        print("\nAvailable cloud backups (oldest -> newest):")
        for i, (obj, name) in enumerate(candidates, start=1):
            size_mb = obj["size"] / (1024 * 1024)
            print(f"  [{i}] {name}  ({size_mb:.1f} MB)")

        choice = input("\nEnter the number of the backup to restore (or blank to cancel): ").strip()
        if choice == "":
            print("Cancelled.")
            return
        if not choice.isdigit() or not (1 <= int(choice) <= len(candidates)):
            print("Invalid selection.")
            return

        obj, name = candidates[int(choice) - 1]
        cloud_download_dir = BACKUP_DIR / ".cloud_downloads"
        print(f"\nDownloading {name} from cloud storage...")
        try:
            downloaded_tmp, name = _download_and_decrypt_cloud_backup(cb, obj["key"], name, cloud_download_dir)
        except Exception as e:
            print(f"Failed to download/prepare backup: {e}")
            send_notification(False, f"host={socket.gethostname()} failed to download/prepare cloud backup {name}: {e}", event_label="Restore")
            for leftover_name in (name, name[:-4] if name.endswith(".enc") else None):
                if leftover_name:
                    leftover = cloud_download_dir / leftover_name
                    if leftover.exists():
                        try:
                            leftover.unlink()
                        except Exception:
                            pass
            return

        m = BACKUP_RE.match(name)
        selected = BackupFile(path=downloaded_tmp, dt=datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y-%m-%d_%H-%M-%S"))
    else:
        backups = list_backups(BACKUP_DIR)
        if not backups:
            print(f"\nNo backups found in {BACKUP_DIR}.")
            return

        print("\nAvailable backups (oldest -> newest):")
        for i, b in enumerate(backups, start=1):
            print(f"  [{i}] {b.path.name}")

        choice = input("\nEnter the number of the backup to restore (or blank to cancel): ").strip()
        if choice == "":
            print("Cancelled.")
            return

        if not choice.isdigit() or not (1 <= int(choice) <= len(backups)):
            print("Invalid selection.")
            return

        selected = backups[int(choice) - 1]

    print(f"\nSelected: {selected.path.name}")
    print("WARNING: This will overwrite /root/docker-compose.yml and completely replace /root/config/.")
    confirm = input("Type YES to confirm (there is no going back): ").strip()
    if confirm != "YES":
        print("Cancelled.")
        return

    restore_tag = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{os.getpid()}"
    tmp_dir = ROOT_DIR / f".restore_tmp_{restore_tag}"
    config_bak = ROOT_DIR / f".config_bak_{restore_tag}"
    compose_bak = ROOT_DIR / f".compose_bak_{restore_tag}.yml"
    stack_stopped = False
    config_staged = False
    compose_replaced = False
    compose_rolled_back = False
    try:
        tmp_dir.mkdir(parents=True, exist_ok=False)

        print(f"\nExtracting {selected.path.name} ...")
        with tarfile.open(selected.path, "r:gz") as tar:
            safe_extract_tar(tar, tmp_dir)

        extracted_compose = tmp_dir / "docker-compose.yml"
        extracted_config = tmp_dir / "config"

        if not extracted_compose.exists():
            print("ERROR: Backup does not contain docker-compose.yml. Aborting.")
            send_notification(False, f"host={socket.gethostname()} backup {selected.path.name} is missing docker-compose.yml", event_label="Restore")
            return
        if not extracted_config.exists() or not extracted_config.is_dir():
            print("ERROR: Backup does not contain a config/ directory. Aborting.")
            send_notification(False, f"host={socket.gethostname()} backup {selected.path.name} is missing config/", event_label="Restore")
            return

        # Preflight succeeded; now stop the stack and perform restore.
        rc = run(["docker", "compose", "down"], cwd=ROOT_DIR)
        if rc != 0:
            print("docker compose down failed; aborting restore.")
            sys.exit(rc)
        stack_stopped = True

        # Stage and replace docker-compose.yml. If no compose exists yet,
        # restore can still proceed but rollback for compose is unavailable.
        if COMPOSE_FILE.exists():
            shutil.copy2(COMPOSE_FILE, compose_bak)
        shutil.copy2(extracted_compose, COMPOSE_FILE)
        compose_replaced = True
        print(f"Restored: {COMPOSE_FILE}")

        # Atomically stage the existing config aside before copying the backup in.
        # rename() is atomic on the same filesystem — no window where config/ is absent.
        if CONFIG_DIR.exists():
            CONFIG_DIR.rename(config_bak)
            config_staged = True
        try:
            shutil.copytree(extracted_config, CONFIG_DIR)
        except Exception as e:
            print(f"ERROR: Failed to copy restored config: {e}")
            # Attempt rollback: remove any partial restore, then put original back.
            if CONFIG_DIR.exists():
                try:
                    shutil.rmtree(CONFIG_DIR)
                except Exception as cleanup_err:
                    print(f"WARNING: Failed to remove partially restored config: {cleanup_err}")
            if config_staged and config_bak.exists():
                try:
                    config_bak.rename(CONFIG_DIR)
                    print("Rolled back: original config/ preserved.")
                except Exception as restore_err:
                    print(f"WARNING: Failed to restore original config from backup: {restore_err}")

            if compose_replaced and compose_bak.exists():
                try:
                    shutil.copy2(compose_bak, COMPOSE_FILE)
                    compose_rolled_back = True
                    print("Rolled back: original docker-compose.yml preserved.")
                except Exception as compose_restore_err:
                    print(f"WARNING: Failed to restore original docker-compose.yml: {compose_restore_err}")
            raise

        print(f"Restored: {CONFIG_DIR}")

    except BaseException as e:
        if compose_replaced and not compose_rolled_back and compose_bak.exists():
            try:
                shutil.copy2(compose_bak, COMPOSE_FILE)
                compose_rolled_back = True
                print("Rolled back: original docker-compose.yml preserved.")
            except Exception as compose_restore_err:
                print(f"WARNING: Failed to restore original docker-compose.yml: {compose_restore_err}")

        if stack_stopped:
            print("\nRestore failed after stack was stopped. Attempting to start services again...")
            up_rc = run(["docker", "compose", "up", "-d"], cwd=ROOT_DIR)
            if up_rc != 0:
                print("WARNING: Failed to restart stack automatically after restore failure.")
        send_notification(False, f"host={socket.gethostname()} restore of {selected.path.name} failed: {e}", event_label="Restore")
        raise

    finally:
        if tmp_dir.exists():
            try:
                shutil.rmtree(tmp_dir)
            except Exception as e:
                print(f"\nWARNING: Failed to remove temporary restore directory {tmp_dir}: {e}")
        if downloaded_tmp is not None and downloaded_tmp.exists():
            try:
                downloaded_tmp.unlink()
                try:
                    downloaded_tmp.parent.rmdir()  # remove .cloud_downloads/ if now empty
                except OSError:
                    pass
            except Exception as e:
                print(f"\nWARNING: Failed to remove downloaded cloud backup file {downloaded_tmp}: {e}")

    if not stack_stopped:
        return

    rc = run(["docker", "compose", "up", "-d"], cwd=ROOT_DIR)
    if rc != 0:
        print("docker compose up -d failed after restore.")

        # Attempt rollback to pre-restore state when startup fails.
        if CONFIG_DIR.exists() and config_staged and config_bak.exists():
            try:
                shutil.rmtree(CONFIG_DIR)
                config_bak.rename(CONFIG_DIR)
                print("Rolled back after startup failure: original config/ restored.")
            except Exception as e:
                print(f"WARNING: Failed to rollback config after startup failure: {e}")

        if compose_replaced and compose_bak.exists():
            try:
                shutil.copy2(compose_bak, COMPOSE_FILE)
                print("Rolled back after startup failure: original docker-compose.yml restored.")
            except Exception as e:
                print(f"WARNING: Failed to rollback docker-compose.yml after startup failure: {e}")

        # Best-effort attempt to restart with rolled-back state.
        retry_rc = run(["docker", "compose", "up", "-d"], cwd=ROOT_DIR)
        if retry_rc != 0:
            print("WARNING: Failed to restart stack after rollback attempt.")

        send_notification(False, f"host={socket.gethostname()} restore of {selected.path.name}: docker compose up -d failed after restore, rollback attempted", event_label="Restore")
        sys.exit(rc)

    # Startup succeeded, cleanup staged backups.
    if config_bak.exists():
        try:
            shutil.rmtree(config_bak)
        except Exception as e:
            print(f"\nWARNING: Failed to remove staged config backup {config_bak}: {e}")

    if compose_bak.exists():
        try:
            compose_bak.unlink()
        except Exception as e:
            print(f"\nWARNING: Failed to remove staged compose backup {compose_bak}: {e}")

    print("\nRestore complete. Stack restarted.")
    send_notification(True, f"host={socket.gethostname()} restored from {selected.path.name}", event_label="Restore")


def do_verify_backup(source_override: str | None = None) -> bool:
    """
    Verifies the latest backup is structurally sound and restorable, WITHOUT
    touching the live Pangolin stack (no docker compose down/up, no file
    swaps) — this is verify_backup_integrity() run against whichever backup
    would actually be used in a real restore right now. Local by default;
    falls back to cloud if no local backups exist, or with source_override.
    Intended for periodic unattended checks (see --verify-backup), so it
    never prompts except for a missing encryption passphrase.
    """
    local_backups = list_backups(BACKUP_DIR)
    source = source_override
    if source is None:
        source = "local" if local_backups else ("cloud" if cloud_backup_ready() else None)

    if source is None:
        print("No backups found locally, and Cloud Backup isn't configured.")
        return False

    if source == "local":
        if not local_backups:
            print(f"No local backups found in {BACKUP_DIR}.")
            return False
        latest = max(local_backups, key=lambda b: b.dt)
        print(f"Verifying latest local backup: {latest.path.name}")
        ok, msg = verify_backup_integrity(latest.path)
        target_desc = latest.path.name

    elif source == "cloud":
        if not cloud_backup_ready():
            print("Cloud Backup isn't configured.")
            return False
        cb = SETTINGS["cloud_backup"]
        prefix = (cb.get("prefix") or "").strip("/")
        cloud_backups = list_cloud_backups(cb, prefix=prefix)
        if not cloud_backups:
            print(f"No cloud backups found (bucket: {cb['bucket']}, prefix: {prefix or '(root)'}).")
            return False
        latest_key, _dt = max(cloud_backups, key=lambda item: item[1])
        name = latest_key.rsplit("/", 1)[-1]
        target_desc = name.removesuffix(".enc")
        print(f"Verifying latest cloud backup: {name}")

        scratch_dir = Path(tempfile.mkdtemp(prefix="pangolin-verify-"))
        try:
            try:
                downloaded, plain_name = _download_and_decrypt_cloud_backup(cb, latest_key, name, scratch_dir)
            except Exception as e:
                print(f"ERROR: Failed to download/prepare backup: {e}")
                send_notification(False, f"host={socket.gethostname()} verify failed: could not fetch cloud backup {name}: {e}", event_label="Verify")
                return False
            ok, msg = verify_backup_integrity(downloaded)
            target_desc = plain_name
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

    else:
        print(f"ERROR: Invalid source '{source}'. Must be local or cloud.")
        return False

    if ok:
        print(f"Verification PASSED: {target_desc}")
    else:
        print(f"Verification FAILED: {target_desc}: {msg}")

    summary = f"host={socket.gethostname()} source={source} backup={target_desc} " + ("verification passed" if ok else f"verification FAILED: {msg}")
    send_notification(ok, summary, event_label="Verify")
    return ok


def settings_edition_select():
    current_display = SETTINGS["pangolin_edition"] or "Auto-detect"
    print("\nPangolin Edition:")
    for i, opt in enumerate(EDITION_OPTIONS, start=1):
        marker = " (Current)" if opt == SETTINGS["pangolin_edition"] else ""
        print(f"  [{i}] {opt}{marker}")
    auto_idx = len(EDITION_OPTIONS) + 1
    marker = " (Current)" if not SETTINGS["pangolin_edition"] else ""
    print(f"  [{auto_idx}] Auto-detect (matches whatever image is currently deployed){marker}")
    val = input(f"Choose number [blank to keep '{current_display}']: ").strip()
    if val == "":
        return
    if val.isdigit() and 1 <= int(val) <= len(EDITION_OPTIONS):
        SETTINGS["pangolin_edition"] = EDITION_OPTIONS[int(val) - 1]
    elif val.isdigit() and int(val) == auto_idx:
        SETTINGS["pangolin_edition"] = ""
    else:
        SETTINGS["pangolin_edition"] = val
    save_settings(SETTINGS)
    print(f"Edition set to: {SETTINGS['pangolin_edition'] or 'Auto-detect'}")

def settings_root_directory():
    val = input(f"\nEnter new Pangolin root directory [blank to keep '{ROOT_DIR}']: ").strip()
    if val == "":
        return
    SETTINGS["root_dir"] = str(Path(val).expanduser())
    save_settings(SETTINGS)
    refresh_paths()
    print(f"Root directory set to: {ROOT_DIR}")

def settings_backup_path():
    current = SETTINGS.get("backup_path") or ""
    prompt_current = current if current else f"{ROOT_DIR / 'backup'} (default)"
    val = input(f"\nEnter new backup path [blank to keep '{prompt_current}', type 'default' to reset]: ").strip()
    if val == "":
        return
    SETTINGS["backup_path"] = "" if val.lower() == "default" else str(Path(val).expanduser())
    save_settings(SETTINGS)
    refresh_paths()
    print(f"Backup path set to: {BACKUP_DIR}")

def _mask_secret_display(val, shown_prefix_len=4):
    if not val:
        return "not set"
    if len(val) <= shown_prefix_len:
        return "set"
    return val[:shown_prefix_len] + "…"

def settings_cloud_field(field, label, secret=False):
    cb = SETTINGS["cloud_backup"]
    if secret:
        val = getpass.getpass(f"\nEnter {label} [blank to keep current]: ")
    else:
        current = cb.get(field) or "not set"
        val = input(f"\nEnter {label} [blank to keep '{current}']: ").strip()
    if val == "":
        return
    cb[field] = val
    save_settings(SETTINGS)
    print(f"{label} updated.")

def settings_cloud_toggle_field(field, label, default=True):
    cb = SETTINGS["cloud_backup"]
    current = cb.get(field, default)
    val = input(f"\n{label}? (Y/N) [current: {'Y' if current else 'N'}]: ").strip().lower()
    if val in ("y", "yes"):
        cb[field] = True
    elif val in ("n", "no"):
        cb[field] = False
    else:
        return
    save_settings(SETTINGS)
    print(f"{label}: {'Yes' if cb[field] else 'No'}")

def settings_cloud_backup_toggle():
    cb = SETTINGS["cloud_backup"]
    if not cb.get("enabled") and not cloud_backup_configured():
        print("\nCloud Backup can't be enabled yet — set Endpoint, Bucket, Access Key, and Secret Key first.")
        return
    val = input(f"\nEnable Cloud Backup? (Y/N) [current: {'Y' if cb.get('enabled') else 'N'}]: ").strip().lower()
    if val in ("y", "yes"):
        cb["enabled"] = True
    elif val in ("n", "no"):
        cb["enabled"] = False
    else:
        return
    save_settings(SETTINGS)
    print(f"Cloud Backup: {'Enabled' if cb['enabled'] else 'Disabled'}")

def settings_cloud_test_connection():
    cb = SETTINGS["cloud_backup"]
    if not cloud_backup_configured():
        print("\nSet Endpoint, Bucket, Access Key, and Secret Key first.")
        return
    print("\nTesting connection (uploading + deleting a small marker object)...")
    try:
        s3_test_connection(cb)
        print("Success: credentials and bucket access look good.")
    except Exception as e:
        print(f"Failed: {e}")

NOTIFICATION_TYPE_OPTIONS = ["discord", "slack", "ntfy", "generic"]

def settings_notification_field(field, label, secret=False):
    n = SETTINGS["notifications"]
    if secret:
        val = getpass.getpass(f"\nEnter {label} [blank to keep current]: ")
    else:
        current = n.get(field) or "not set"
        val = input(f"\nEnter {label} [blank to keep '{current}']: ").strip()
    if val == "":
        return
    n[field] = val
    save_settings(SETTINGS)
    print(f"{label} updated.")

def settings_notification_toggle_field(field, label, default=True):
    n = SETTINGS["notifications"]
    current = n.get(field, default)
    val = input(f"\n{label}? (Y/N) [current: {'Y' if current else 'N'}]: ").strip().lower()
    if val in ("y", "yes"):
        n[field] = True
    elif val in ("n", "no"):
        n[field] = False
    else:
        return
    save_settings(SETTINGS)
    print(f"{label}: {'Yes' if n[field] else 'No'}")

def settings_notifications_type_select():
    n = SETTINGS["notifications"]
    print("\nNotification provider type:")
    for i, opt in enumerate(NOTIFICATION_TYPE_OPTIONS, start=1):
        marker = " (Current)" if opt == n.get("type") else ""
        print(f"  [{i}] {opt}{marker}")
    val = input(f"Choose number [blank to keep '{n.get('type') or 'not set'}']: ").strip()
    if val == "":
        return
    if val.isdigit() and 1 <= int(val) <= len(NOTIFICATION_TYPE_OPTIONS):
        n["type"] = NOTIFICATION_TYPE_OPTIONS[int(val) - 1]
        save_settings(SETTINGS)
        print(f"Provider type set to: {n['type']}")
    else:
        print("Invalid choice.")

def settings_notifications_toggle():
    n = SETTINGS["notifications"]
    if not n.get("enabled") and not notifications_configured():
        print("\nNotifications can't be enabled yet — set Provider Type and its Webhook/Topic URL first.")
        return
    val = input(f"\nEnable Notifications? (Y/N) [current: {'Y' if n.get('enabled') else 'N'}]: ").strip().lower()
    if val in ("y", "yes"):
        n["enabled"] = True
    elif val in ("n", "no"):
        n["enabled"] = False
    else:
        return
    save_settings(SETTINGS)
    print(f"Notifications: {'Enabled' if n['enabled'] else 'Disabled'}")

def settings_notifications_test():
    if not notifications_configured():
        print("\nSet Provider Type and its Webhook/Topic URL first.")
        return
    print("\nSending a test notification...")
    try:
        _send_notification_payload(SETTINGS["notifications"], True, "Test notification from pangolin-updater Settings.", event_label="Test")
        print("Success: notification sent.")
    except Exception as e:
        print(f"Failed: {e}")

def do_settings_notifications():
    while True:
        render_screen("Settings > Notifications")
        n = SETTINGS["notifications"]
        url_field = "ntfy_topic_url" if n.get("type") == "ntfy" else "webhook_url"
        url_label = "Topic URL" if n.get("type") == "ntfy" else "Webhook URL"
        print(f"[1] Enable/Disable          (current: {'Enabled' if n.get('enabled') else 'Disabled'})")
        print(f"[2] Provider Type           (current: {n.get('type') or 'not set'})")
        print(f"[3] {url_label:<27} (current: {'set' if n.get(url_field) else 'not set'})")
        print(f"[4] Notify on Success       (current: {'Yes' if n.get('notify_on_success', True) else 'No'})")
        print(f"[5] Notify on Failure       (current: {'Yes' if n.get('notify_on_failure', True) else 'No'})")
        print("[6] Send Test Notification")
        print("[7] Back to Settings")
        if not notifications_configured():
            print("\nNote: Provider Type and its Webhook/Topic URL are required before enabling.")
        choice = input("Select an option [1-7]: ").strip()

        if choice == "1":
            settings_notifications_toggle()
            pause()
        elif choice == "2":
            settings_notifications_type_select()
            pause()
        elif choice == "3":
            settings_notification_field(url_field, url_label)
            pause()
        elif choice == "4":
            settings_notification_toggle_field("notify_on_success", "Notify on Success")
            pause()
        elif choice == "5":
            settings_notification_toggle_field("notify_on_failure", "Notify on Failure")
            pause()
        elif choice == "6":
            settings_notifications_test()
            pause()
        elif choice == "7":
            return
        else:
            print("Invalid option.")
            pause()

def do_settings_cloud_backup():
    while True:
        render_screen("Settings > Cloud Backup")
        cb = SETTINGS["cloud_backup"]
        print(f"[1]  Enable/Disable          (current: {'Enabled' if cb.get('enabled') else 'Disabled'})")
        print(f"[2]  Endpoint URL            (current: {cb.get('endpoint') or 'not set'})")
        print(f"[3]  Bucket                  (current: {cb.get('bucket') or 'not set'})")
        print(f"[4]  Access Key              (current: {_mask_secret_display(cb.get('access_key'))})")
        print(f"[5]  Secret Key              (current: {'set' if cb.get('secret_key') else 'not set'})")
        print(f"[6]  Region                  (current: {cb.get('region') or 'us-east-1'})")
        print(f"[7]  Path Prefix             (current: {cb.get('prefix') or '(bucket root)'})")
        print(f"[8]  Path-style addressing   (current: {'Yes' if cb.get('use_path_style', True) else 'No'})")
        print(f"[9]  Verify SSL certificate  (current: {'Yes' if cb.get('verify_ssl', True) else 'No'})")
        print(f"[10] Encrypt cloud backups   (current: {'Enabled' if cb.get('encrypt_cloud_backups') else 'Disabled'})")
        print(f"[11] Encryption Passphrase   (current: {'set' if cb.get('encryption_passphrase') else 'not set'})")
        print("[12] Test Connection")
        print("[13] Back to Settings")
        if not cloud_backup_configured():
            print("\nNote: Endpoint, Bucket, Access Key, and Secret Key are all required before enabling.")
        choice = input("Select an option [1-13]: ").strip()

        if choice == "1":
            settings_cloud_backup_toggle()
            pause()
        elif choice == "2":
            settings_cloud_field("endpoint", "Endpoint URL (e.g. https://minio.example.com:9000)")
            pause()
        elif choice == "3":
            settings_cloud_field("bucket", "Bucket name")
            pause()
        elif choice == "4":
            settings_cloud_field("access_key", "Access Key")
            pause()
        elif choice == "5":
            settings_cloud_field("secret_key", "Secret Key", secret=True)
            pause()
        elif choice == "6":
            settings_cloud_field("region", "Region (MinIO default: us-east-1)")
            pause()
        elif choice == "7":
            settings_cloud_field("prefix", "Path prefix (e.g. pangolin/, blank for bucket root)")
            pause()
        elif choice == "8":
            settings_cloud_toggle_field("use_path_style", "Use path-style addressing (required by most MinIO setups)")
            pause()
        elif choice == "9":
            settings_cloud_toggle_field("verify_ssl", "Verify SSL certificate (disable only for self-signed MinIO)")
            pause()
        elif choice == "10":
            settings_cloud_toggle_field("encrypt_cloud_backups", "Encrypt cloud backups (requires openssl on this system)")
            pause()
        elif choice == "11":
            settings_cloud_field("encryption_passphrase", "Encryption Passphrase", secret=True)
            pause()
        elif choice == "12":
            settings_cloud_test_connection()
            pause()
        elif choice == "13":
            return
        else:
            print("Invalid option.")
            pause()

def do_settings():
    while True:
        render_screen("Settings")
        backup_display = str(BACKUP_DIR)
        if not SETTINGS.get("backup_path"):
            backup_display += "  (default)"
        cb = SETTINGS["cloud_backup"]
        if cb.get("enabled"):
            cb_display = "Enabled"
        elif cloud_backup_configured():
            cb_display = "Configured (disabled)"
        else:
            cb_display = "Not configured"
        n = SETTINGS["notifications"]
        if n.get("enabled"):
            n_display = "Enabled"
        elif notifications_configured():
            n_display = "Configured (disabled)"
        else:
            n_display = "Not configured"
        print(f"[1] Pangolin Edition Select   (current: {SETTINGS['pangolin_edition'] or 'Auto-detect'})")
        print(f"[2] Pangolin Root Directory   (current: {ROOT_DIR})")
        print(f"[3] Backup Path               (current: {backup_display})")
        print(f"[4] Cloud Backup              (current: {cb_display})")
        print(f"[5] Notifications             (current: {n_display})")
        print("[6] Back to Main Menu")
        choice = input("Select an option [1-6]: ").strip()

        if choice == "1":
            settings_edition_select()
            pause()
        elif choice == "2":
            settings_root_directory()
            pause()
        elif choice == "3":
            settings_backup_path()
            pause()
        elif choice == "4":
            do_settings_cloud_backup()
        elif choice == "5":
            do_settings_notifications()
        elif choice == "6":
            return
        else:
            print("Invalid option.")
            pause()

def main():
    handle_cli_flags()
    require_root()

    newer_version = check_for_update()

    while True:
        render_screen("Main Menu")
        if newer_version:
            print(ui_text(f"Update available: v{__version__} -> v{newer_version}  (run: updater --update)", color=ANSI_CYAN, bold=True))
            print()
        print("[1] Backup stack and config")
        print("[2] Update image versions")
        print("[3] Restore from backup")
        print("[4] Settings")
        print("[5] Close")
        choice = input("Select an option [1-5]: ").strip()

        if choice == "1":
            do_backup()
            pause()
        elif choice == "2":
            do_update()
            pause()
        elif choice == "3":
            do_restore()
            pause()
        elif choice == "4":
            do_settings()
        elif choice == "5":
            print("Bye.")
            return
        else:
            print("Invalid option.")
            pause()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        sys.exit(130)
