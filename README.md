# Pangolin Updater (CLI)

A CLI utility to manage a root-run Pangolin Docker Compose stack in `/root`.

It is designed for environments where:
- `/root/docker-compose.yml` defines `pangolin`, `gerbil`, and `traefik`
- `/root/config/` contains Pangolin and related runtime data
- Services are managed with `docker compose`

---

## What it does

### 1) Backup
- Creates `/root/backup/` if needed
- Creates timestamped archives: `pangolin-backup-YYYY-MM-DD_HH-MM-SS.tar.gz`
- Archives:
  - `/root/docker-compose.yml` as `docker-compose.yml`
  - `/root/config/` as `config/`
- Applies backup retention policy automatically
- Optionally cleans up `/root/docker-compose.yml.bak.*` safety files

### 2) Update
1. Optionally creates a backup first (recommended)
2. Reads current pinned tags from `/root/docker-compose.yml`
3. Fetches stable GitHub release tags for Pangolin, Gerbil, and Traefik (excluding prerelease/draft, `-rc`, and `-ea`)
4. Shows a numbered selector per service:
  - Upgrades listed first
  - Current version near the bottom, labeled `(Current)`, and selected by default on Enter
  - One downgrade option
  - Release page links shown inline
  - Pangolin includes a callout recommending upgrade-by-upgrade rollout with backup/testing at each step
5. Shows planned changes and exits early if everything is unchanged
6. On confirmation, writes compose update (with a timestamped `.bak` safety copy), then only pulls and recreates the services that actually changed (leaving the rest of the stack running):
  - `docker compose pull <changed services>`
  - `docker compose up -d <changed services>`
7. Optionally prunes unused Docker images

### 3) Restore
1. Lists available backups from `/root/backup/`
2. Lets you choose one and requires explicit `YES` confirmation
3. Pre-validates/extracts archive contents to a temp directory safely
4. Stops stack with `docker compose down`
5. Restores:
  - `/root/docker-compose.yml`
  - `/root/config/` (replaced using staged rollback protection)
6. Starts stack again with `docker compose up -d`
7. Cleans temp extraction files
8. If restore fails after stopping containers, it attempts to bring the stack back up automatically

### 4) Close
Exits the tool.

---

## Requirements
- Linux host
- Docker installed and running
- Docker Compose v2 (`docker compose ...`)
- Run as root (uses `/root` paths and manages Docker)

---

## Paths used

Expected:
- `/root/docker-compose.yml`
- `/root/config/`

Created/used:
- `/root/backup/`

---

## Usage
```bash
updater
```

Menu:
- `[1] Backup`
- `[2] Update`
- `[3] Restore`
- `[4] Settings`
- `[5] Close`

A "Update available" banner appears on the main menu when a newer version is published (checked at most once/day, silent on network failure).

### Self-update
```bash
updater --update
```
Downloads and installs the latest version from GitHub in place. No-ops if already up to date; add `--force` to reinstall anyway.

### Scheduled backups (cron)
```bash
updater --backup
```
Runs a full backup with no interactive prompts — creates the archive, verifies its integrity, uploads to Cloud Backup if configured (encrypting first if enabled), applies local and cloud retention, and sends a webhook notification if one is configured. Exits `0` on success, non-zero on failure, so it's safe to alert on in a scheduler.

By default it uses Cloud Backup + Local if Cloud Backup is enabled in Settings, otherwise Local only. Override explicitly with:
```bash
updater --backup --destination=local   # local only
updater --backup --destination=cloud   # cloud only
updater --backup --destination=both    # local + cloud
```

Cloud Backup, encryption, and Notifications are all configured ahead of time via `updater`'s interactive `Settings` menu — `--backup` just uses whatever's already set there, since it never prompts.

This isn't installed automatically — add it yourself via `crontab -e` (as root, since the updater requires root). Example: run daily at 3:00 AM, with output logged for later inspection:
```cron
0 3 * * * /usr/local/bin/updater --backup >> /var/log/pangolin-backup.log 2>&1
```
Or every 6 hours:
```cron
0 */6 * * * /usr/local/bin/updater --backup >> /var/log/pangolin-backup.log 2>&1
```

### Backup verification
```bash
updater --verify-backup
```
Confirms the latest backup is actually restorable — downloads it (decrypting first if needed) and runs the same integrity check `--backup` runs right after creating an archive, but **never touches the live stack**: no `docker compose down`/`up`, no file swaps. Verifies the latest local backup by default, falling back to the latest cloud backup if no local backups exist; override with `--source=local` or `--source=cloud`. Exits `0` if the backup verified cleanly, non-zero otherwise, and sends a webhook notification if one is configured — good for a periodic sanity check that your disaster-recovery backups actually work, not just that they exist:
```cron
0 4 * * 0 /usr/local/bin/updater --verify-backup >> /var/log/pangolin-verify.log 2>&1
```

### Checking for image updates
```bash
updater --check-updates
```
Reports whether a stable upgrade is available for Pangolin, Gerbil, or Traefik, using the exact same rules the interactive `Update` menu uses (Traefik's v3 lock, Pangolin edition awareness, RC/beta filtering) — **but never applies anything.** This is check-only, on purpose: image updates can involve breaking changes or migrations, so applying them is left to the interactive menu where you can review what's changing first.

Sends a notification only when there's something to know (updates found, or the check itself failed) — silent when everything's current. Exit codes: `0` up to date, `1` updates available, `2` the check failed. Good for a daily heads-up without any risk of an unattended update:
```cron
0 8 * * * /usr/local/bin/updater --check-updates >> /var/log/pangolin-check-updates.log 2>&1
```

### Status snapshot
```bash
updater --status
```
A quick, read-only diagnostic dump: running image versions, Pangolin Edition setting, local backup count/age/disk space, cloud backup freshness (if configured), Notifications state, and whether a newer version of the updater itself is available. Never modifies anything — safe to run anytime, including from a monitoring script.

---

## Installation

### One-line install (new users)
```bash
curl -fsSL https://raw.githubusercontent.com/davetech-dev/Pangolin-Updater/main/install.sh | sudo bash
```

Force reinstall even when installed version is equal/newer:
```bash
curl -fsSL https://raw.githubusercontent.com/davetech-dev/Pangolin-Updater/main/install.sh | sudo bash -s -- --force
```

### One-line install (recommended)
```bash
curl -fsSL https://raw.githubusercontent.com/davetech-dev/Pangolin-Updater/main/install.sh | sudo bash
```

Force reinstall even when installed version is equal/newer:
```bash
curl -fsSL https://raw.githubusercontent.com/davetech-dev/Pangolin-Updater/main/install.sh | sudo bash -s -- --force
```

Verify:
```bash
which updater
updater --version
```

### Manual install
```bash
sudo install -m 0755 ./pangolin_updater.py /usr/local/bin/updater
```

### Uninstall
```bash
chmod +x uninstall.sh
sudo ./uninstall.sh
```

---

## Troubleshooting

### "This tool must be run as root"
```bash
sudo updater
```

### "Missing /root/docker-compose.yml" or "Missing /root/config directory"
Ensure both paths exist.

### Docker command failures
Try the command manually:
```bash
cd /root
docker compose up -d
```
