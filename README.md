# Pangolin Updater (CLI)

A simple command-line tool to **backup** and **update** a root-run Pangolin Docker Compose stack located in `/root`.

It is designed for a setup where:
- `/root/docker-compose.yml` defines the services (pangolin, gerbil, traefik)
- `/root/config/` contains the Pangolin database and other configuration (Traefik config, letsencrypt, etc.)
- The stack is managed with `docker compose`

---

## Table of Contents
- [What this tool does](#what-this-tool-does)
  - [1) Backup](#1-backup)
  - [2) Update](#2-update)
  - [3) Close](#3-close)
- [Requirements](#requirements)
- [Files and directories used](#files-and-directories-used)
- [How to use](#how-to-use)
  - [Restore from a backup (manual)](#restore-from-a-backup-manual)
- [Installation](#installation)
  - [Install via install script (recommended)](#install-via-install-script-recommended)
  - [Manual install (copy)](#manual-install-copy)
- [Safety notes / best practices](#safety-notes--best-practices)
- [Troubleshooting](#troubleshooting)

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
