# Pangolin Updater (CLI)

A simple command-line tool to **backup** and **update** a root-run Pangolin Docker Compose stack located in `/root`.

It is designed for a setup where:
- `/root/docker-compose.yml` defines the services (pangolin, gerbil, traefik)
- `/root/config/` contains the Pangolin database and other configuration (Traefik config, letsencrypt, etc.)
- The stack is managed with `docker compose`

---

## What this tool does

### 1) Backup
When you choose **Backup**, it:
- Creates `/root/backup/` if it doesn’t exist
- Creates a timestamped tarball like:

  `pangolin-backup-YYYY-MM-DD_HH-MM-SS.tar.gz`

- The tarball contains:
  - `/root/docker-compose.yml` (stored as `docker-compose.yml` in the archive)
  - `/root/config/` (stored as `config/` in the archive)

Backup location:
- `/root/backup`

---

### 2) Update
When you choose **Update**, it:
1. Reads current pinned image tags from `/root/docker-compose.yml`:
   - `fosrl/pangolin:<tag>`
   - `fosrl/gerbil:<tag>`
   - `traefik:<tag>`

2. Prompts you to enter a tag for each service:
   - Press **Enter** to keep the current version unchanged
   - Otherwise it will replace the tag in the compose file

3. Prints a summary showing for each service:
   - old tag -> new tag
   - Upgrade / Downgrade / Unchanged (best-effort)

4. Asks you to confirm (Y/N). Default is **N**.

5. If confirmed, it:
   - Saves a safety copy of the compose file (timestamped `.bak...`)
   - Writes the updated `/root/docker-compose.yml`
   - Runs:
     - `docker compose down`
     - `docker compose up -d`

6. Optional cleanup:
   - Prompts whether to prune images (your script may be configured to default Yes)
   - Runs Docker prune commands to remove unused images

> Note: Docker image pruning only removes images not used by any containers. If an image is still referenced by a container, it won’t be removed.

---

### 3) Close
Exits the program.

---

## Requirements

- Linux host with Docker installed
- Docker Compose v2 (i.e. `docker compose ...` works)
- Run as **root** (the tool expects files under `/root` and manages Docker)

---

## Files and directories used

Expected:
- `/root/docker-compose.yml`
- `/root/config/`

Created/used:
- `/root/backup/` (backup tarballs stored here)

---

## How to use

Run the command (once installed):
```bash
updater
```

You’ll see a menu:
- `[1] Backup`
- `[2] Update`
- `[3] Close`

### Restore from a backup (manual)
To restore, you typically:
1. Stop the stack:
```bash
cd /root
docker compose down
```

2. Extract a backup tarball into `/root`:
```bash
cd /root
tar -xzf /root/backup/pangolin-backup-YYYY-MM-DD_HH-MM-SS.tar.gz
```

3. Start the stack again:
```bash
cd /root
docker compose up -d
```

---

## Installation

### Install via install script (recommended)
Assumptions:
- Your main script file is named: `pangolin_updater`
- You want the command name to be: `updater`

1. Make the install script executable:
```bash
chmod +x install.sh
```

2. Run it as root:
```bash
sudo ./install.sh
```

3. Verify:
```bash
which updater
updater
```

### Manual install (copy)
If you prefer not to use `install.sh`:
```bash
sudo install -m 0755 ./pangolin_updater /usr/local/bin/updater
```

---

## Safety notes / best practices

- The tool modifies `/root/docker-compose.yml`. It also creates a timestamped safety backup of the compose file before writing changes.
- Always ensure you have a recent backup before changing versions.
- Pruning images can remove old versions you may want to roll back to; keep backups if rollback matters.

---

## Troubleshooting

### “This tool must be run as root”
Run with:
```bash
sudo updater
```

### “Missing /root/docker-compose.yml” or “Missing /root/config directory”
Confirm your compose file and config folder exist and are in `/root`.

### Docker commands fail
Try running the same command manually to see the error, e.g.:
```bash
cd /root
docker compose up -d
```

Then fix the underlying Docker/Compose problem (permissions, daemon status, invalid compose file, etc.).
