#!/usr/bin/env python3
import os
import re
import sys
import tarfile
import shutil
import subprocess
import threading
import time
import json
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta

__app_name__ = "pangolin-updater"
__version__ = "0.1.2"

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


# Settings file lives at a fixed location independent of root_dir, since
# root_dir itself is one of the settings it stores.
SETTINGS_FILE = Path("/etc/pangolin-updater/settings.json")

DEFAULT_SETTINGS = {
    "pangolin_edition": "",  # empty = auto-detect from the currently deployed image tag
    "root_dir": "/root",
    "backup_path": "",  # empty = derive from root_dir/backup
    "cloud_backup": {
        "enabled": False,
        "provider": "",
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
    return merged

def save_settings(settings):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

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
  updater --help       Show help
""")
        sys.exit(0)

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

def do_backup(render: bool = True):
    if render:
        render_screen("Backup")
    require_paths()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_name = f"pangolin-backup-{ts}.tar.gz"
    backup_path = BACKUP_DIR / backup_name

    with tarfile.open(backup_path, "w:gz", compresslevel=6) as tar:
        tar.add(str(COMPOSE_FILE), arcname="docker-compose.yml")
        tar.add(str(CONFIG_DIR), arcname="config", filter=_backup_tar_filter)

    print(f"\nBackup created: {backup_path}")
    print("\nApplying backup retention policy in /root/backup ...")
    kept, deleted = apply_backup_retention(BACKUP_DIR)
    print(f"Retention done. Kept: {len(kept)}  Deleted: {len(deleted)}")

    cleanup_baks = input("\nCleanup all docker-compose .bak files in /root now? (Y/N) [default: N]: ").strip().lower()
    if cleanup_baks in ("y", "yes"):
        removed = cleanup_compose_bak_files()
        print(f"Removed compose backups: {removed}")

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

def apply_backup_retention(backup_dir: Path, now: datetime | None = None, dry_run: bool = False) -> tuple[list[Path], list[Path]]:
    """
    Returns (kept_paths, deleted_paths)
    """
    if now is None:
        now = datetime.now()

    backups = list_backups(backup_dir)
    if not backups:
        return ([], [])

    # Group by day/week/month
    by_day = defaultdict(list)
    by_week = defaultdict(list)   # (iso_year, iso_week)
    by_month = defaultdict(list)  # (year, month)

    for b in backups:
        day_key = b.dt.date()
        iso_year, iso_week, _ = b.dt.isocalendar()
        week_key = (iso_year, iso_week)
        month_key = (b.dt.year, b.dt.month)

        by_day[day_key].append(b)
        by_week[week_key].append(b)
        by_month[month_key].append(b)

    # Helper: latest in group
    def latest(group: list[BackupFile]) -> BackupFile:
        return max(group, key=lambda x: x.dt)

    keep = set()

    today = now.date()

    # 1) Keep ALL from today
    for b in by_day.get(today, []):
        keep.add(b.path)

    # 2) Keep latest from each of previous 3 days
    for delta in (1, 2, 3):
        d = (now.date() - timedelta(days=delta))
        if d in by_day:
            keep.add(latest(by_day[d]).path)

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
            keep.add(latest(by_week[wk]).path)

    # 4) For older backups (anything not already covered), keep latest per month
    # “Older” here means: not today, not in last 3 days, and not in the two previous weeks.
    covered_days = {today, today - timedelta(days=1), today - timedelta(days=2), today - timedelta(days=3)}
    covered_weeks = prev_week_keys | {(current_iso_year, current_iso_week)}

    for month_key, group in by_month.items():
        # Determine if this month group contains any backup outside the covered windows.
        # If the month has *only* covered backups, monthly retention isn’t needed.
        has_older = False
        for b in group:
            d = b.dt.date()
            iso_year, iso_week, _ = b.dt.isocalendar()
            if (d not in covered_days) and ((iso_year, iso_week) not in covered_weeks):
                has_older = True
                break

        if has_older:
            keep.add(latest(group).path)

    kept = sorted(list(keep))
    deleted = [b.path for b in backups if b.path not in keep]

    if not dry_run:
        for p in deleted:
            try:
                p.unlink()
            except Exception as e:
                print(f"Warning: failed to delete backup {p}: {e}")

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
        return

    write_compose_text(new_text)

    # Only pull + recreate the services that actually changed, leaving the
    # rest of the stack running undisturbed.
    print(f"\nRestarting only: {', '.join(changed_services)}")
    rc = run(["docker", "compose", "pull"] + changed_services, cwd=ROOT_DIR)
    if rc != 0:
        print("docker compose pull failed; aborting.")
        sys.exit(rc)

    rc = run(["docker", "compose", "up", "-d"] + changed_services, cwd=ROOT_DIR)
    if rc != 0:
        print("docker compose up -d failed.")
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

def do_restore():
    render_screen("Restore")
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
            return
        if not extracted_config.exists() or not extracted_config.is_dir():
            print("ERROR: Backup does not contain a config/ directory. Aborting.")
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

    except BaseException:
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
        raise

    finally:
        if tmp_dir.exists():
            try:
                shutil.rmtree(tmp_dir)
            except Exception as e:
                print(f"\nWARNING: Failed to remove temporary restore directory {tmp_dir}: {e}")

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

def settings_cloud_backup():
    cb = SETTINGS["cloud_backup"]
    print(f"\nCloud Backup is currently: {'Enabled' if cb.get('enabled') else 'Disabled'}")
    print("(Provider integration not yet implemented — this only stores your preference.)")
    val = input("Enable cloud backup? (Y/N) [blank to keep current]: ").strip().lower()
    if val in ("y", "yes"):
        cb["enabled"] = True
    elif val in ("n", "no"):
        cb["enabled"] = False
    else:
        return
    save_settings(SETTINGS)
    print(f"Cloud Backup set to: {'Enabled' if cb['enabled'] else 'Disabled'}")

def do_settings():
    while True:
        render_screen("Settings")
        backup_display = str(BACKUP_DIR)
        if not SETTINGS.get("backup_path"):
            backup_display += "  (default)"
        cb_display = "Enabled" if SETTINGS["cloud_backup"].get("enabled") else "Disabled"
        print(f"[1] Pangolin Edition Select   (current: {SETTINGS['pangolin_edition'] or 'Auto-detect'})")
        print(f"[2] Pangolin Root Directory   (current: {ROOT_DIR})")
        print(f"[3] Backup Path               (current: {backup_display})")
        print(f"[4] Cloud Backup              (current: {cb_display})")
        print("[5] Back to Main Menu")
        choice = input("Select an option [1-5]: ").strip()

        if choice == "1":
            settings_edition_select()
        elif choice == "2":
            settings_root_directory()
        elif choice == "3":
            settings_backup_path()
        elif choice == "4":
            settings_cloud_backup()
        elif choice == "5":
            return
        else:
            print("Invalid option.")

def main():
    handle_cli_flags()
    require_root()

    while True:
        render_screen("Main Menu")
        print("[1] Backup stack and config")
        print("[2] Update image versions")
        print("[3] Restore from backup")
        print("[4] Settings")
        print("[5] Close")
        choice = input("Select an option [1-5]: ").strip()

        if choice == "1":
            do_backup()
        elif choice == "2":
            do_update()
        elif choice == "3":
            do_restore()
        elif choice == "4":
            do_settings()
        elif choice == "5":
            print("Bye.")
            return
        else:
            print("Invalid option.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        sys.exit(130)
