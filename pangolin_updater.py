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
__version__ = "0.1.1"


ROOT_DIR = Path("/root")
COMPOSE_FILE = ROOT_DIR / "docker-compose.yml"
CONFIG_DIR = ROOT_DIR / "config"
BACKUP_DIR = ROOT_DIR / "backup"
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

    print(f"\n> {' '.join(cmd)} (cwd={cwd})")

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

    t = threading.Thread(target=spinner, daemon=True)
    t.start()

    p = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

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
    def norm(t):
        return t[1:] if t and t.startswith("v") else t

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
    return f"{tag} (current)"

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
        if "-rc" in tag_l or "-ea" in tag_l:
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
        # Exclude links/dev nodes in fallback mode to avoid link-based escapes.
        if member.issym() or member.islnk() or member.isdev():
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

def select_release_tag(meta, current_tag):
    display = meta["display"]
    github_repo = meta.get("github_repo")
    release_url = meta.get("release_url")
    upgrade_note = meta.get("upgrade_note")

    if current_tag is None:
        val = input(f"Enter {display} version tag to pin (current not detected) [leave blank to keep]: ").strip()
        return val if val else current_tag

    print(f"\n{display} versions:")
    if release_url:
        print(f"  Releases: {release_url}")
    if upgrade_note:
        print(f"  NOTE: {upgrade_note}")

    if not github_repo:
        print("  Release source not configured.")
        print(f"  [0] {style_current_tag(current_tag)} (Current)")
        val = input(f"Choose number [default: 0], or type tag manually: ").strip()
        if val == "":
            return current_tag
        return val

    try:
        release_tags = fetch_github_release_tags(github_repo)
    except urllib.error.URLError as e:
        print(f"  Failed to fetch releases: {e}")
        print(f"  [0] {style_current_tag(current_tag)} (Current)")
        val = input(f"Choose number [default: 0], or type tag manually: ").strip()
        if val == "":
            return current_tag
        return val
    except Exception as e:
        print(f"  Failed to parse releases: {e}")
        print(f"  [0] {style_current_tag(current_tag)} (Current)")
        val = input(f"Choose number [default: 0], or type tag manually: ").strip()
        if val == "":
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

    upgrades = [t for t in unique_tags if compare_versions(t, current_tag) > 0]
    downgrades = [t for t in unique_tags if compare_versions(t, current_tag) < 0]

    # Sort upgrades newest first.
    upgrades.sort(key=lambda t: parse_version_tuple(t), reverse=True)

    # Keep only one downgrade: nearest lower version.
    one_downgrade = None
    if downgrades:
        one_downgrade = max(downgrades, key=lambda t: parse_version_tuple(t))

    option_map = {}
    idx = 1
    for tag in upgrades:
        option_map[idx] = tag
        print(f"  [{idx}] {tag} (Upgrade)")
        idx += 1

    current_idx = idx
    option_map[current_idx] = current_tag
    print(f"  [{current_idx}] {style_current_tag(current_tag)} (Current)")
    idx += 1

    if one_downgrade is not None:
        option_map[idx] = one_downgrade
        print(f"  [{idx}] {one_downgrade} (Downgrade)")

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

def do_backup():
    require_paths()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_name = f"pangolin-backup-{ts}.tar.gz"
    backup_path = BACKUP_DIR / backup_name

    with tarfile.open(backup_path, "w:gz") as tar:
        tar.add(str(COMPOSE_FILE), arcname="docker-compose.yml")
        tar.add(str(CONFIG_DIR), arcname="config")

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
    require_paths()

    backup_ans = input("Take a backup before updating? (Y/N) [default: Y]: ").strip().lower()
    if backup_ans in ("", "y", "yes"):
        do_backup()

    compose_text = read_compose_text()
    current = parse_current_tags(compose_text)

    if any(v is None for v in current.values()):
        print("Warning: Could not detect one or more image tags from docker-compose.yml.")
        print("Detected tags:", current)

    print("\nChecking GitHub releases and preparing version choices...")

    selections = {}
    for key, meta in IMAGES.items():
        old = current.get(key)
        selections[key] = select_release_tag(meta, old)

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
    applied_changes = 0
    for key, meta in IMAGES.items():
        old = current.get(key)
        new = selections.get(key)
        if old != new and new is not None:
            try:
                new_text = update_image_tag(new_text, meta["image_repo"], new)
                applied_changes += 1
            except RuntimeError as e:
                print(f"Warning: {e} — skipping {meta['display']}.")

    if applied_changes == 0:
        print("\nNo updates could be applied to docker-compose.yml. Skipping restart.")
        return

    write_compose_text(new_text)

    # Restart stack
    rc = run(["docker", "compose", "down"], cwd=ROOT_DIR)
    if rc != 0:
        print("docker compose down failed; aborting.")
        sys.exit(rc)

    rc = run(["docker", "compose", "up", "-d"], cwd=ROOT_DIR)
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
    stack_stopped = False
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

        # Replace docker-compose.yml
        shutil.copy2(extracted_compose, COMPOSE_FILE)
        print(f"Restored: {COMPOSE_FILE}")

        # Atomically stage the existing config aside before copying the backup in.
        # rename() is atomic on the same filesystem — no window where config/ is absent.
        if CONFIG_DIR.exists():
            CONFIG_DIR.rename(config_bak)
        try:
            shutil.copytree(extracted_config, CONFIG_DIR)
            if config_bak.exists():
                shutil.rmtree(config_bak)
        except Exception as e:
            print(f"ERROR: Failed to copy restored config: {e}")
            # Attempt rollback: remove any partial restore, then put original back.
            if CONFIG_DIR.exists():
                try:
                    shutil.rmtree(CONFIG_DIR)
                except Exception as cleanup_err:
                    print(f"WARNING: Failed to remove partially restored config: {cleanup_err}")
            if config_bak.exists():
                try:
                    config_bak.rename(CONFIG_DIR)
                    print("Rolled back: original config/ preserved.")
                except Exception as restore_err:
                    print(f"WARNING: Failed to restore original config from backup: {restore_err}")
            raise

        print(f"Restored: {CONFIG_DIR}")

    except BaseException:
        if stack_stopped:
            print("\nRestore failed after stack was stopped. Attempting to start services again...")
            up_rc = run(["docker", "compose", "up", "-d"], cwd=ROOT_DIR)
            if up_rc != 0:
                print("WARNING: Failed to restart stack automatically after restore failure.")
        raise

    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        if config_bak.exists():
            print(f"\nWARNING: Staged config backup still exists at {config_bak}")
            print(f"  If config/ is absent, restore it manually: mv {config_bak} {CONFIG_DIR}")

    if not stack_stopped:
        return

    rc = run(["docker", "compose", "up", "-d"], cwd=ROOT_DIR)
    if rc != 0:
        print("docker compose up -d failed after restore.")
        sys.exit(rc)

    print("\nRestore complete. Stack restarted.")


def main():
    handle_cli_flags()
    require_root()

    while True:
        print(f"\n=== Pangolin Maintenance Tool v{__version__} ===")
        print("[1] Backup")
        print("[2] Update")
        print("[3] Restore")
        print("[4] Close")
        choice = input("Select an option: ").strip()

        if choice == "1":
            do_backup()
        elif choice == "2":
            do_update()
        elif choice == "3":
            do_restore()
        elif choice == "4":
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
