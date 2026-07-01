# Factorio Server Manager

A lightweight web interface for managing a Factorio dedicated server running under systemd. Built with Flask — no external databases, no heavy dependencies.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Flask](https://img.shields.io/badge/Flask-3.0%2B-green)
![License](https://img.shields.io/badge/license-GPLv3-blue)

---

## Features

### Dashboard
- Live server status (Online / Offline) with auto-refresh every 3 seconds
- Server version display with update-available notification
- Active save file name
- Server uptime counter
- Countdown banner when a shutdown or restart is in progress
- Browser tab favicon (orange gear)

### Server Controls
- **Start / Stop / Restart** the Factorio service with one click
- **Graceful shutdown / restart**: if players are online, warns them in-game via RCON at 20 s, 15 s, 10 s, 5 s, 4 s, 3 s, 2 s, and 1 s before the action executes
- Concurrent control requests are rejected while a countdown is already running

### Server Install / Update
- Choose between **stable** and **experimental** channels
- Toggle **Space Age DLC mods** on or off independently of the channel — writes `mod-list.json` after installation to enable or disable the `space-age`, `elevated-rails`, `recycler`, and `quality` mods
- One-click install when Factorio is not yet present — generates a default `game.zip` save and creates the systemd service automatically
- **Automatic save backup** before every update
- **Downgrade protection**: downgrades are blocked outright — wipe Factorio first, then install the older version
- Live streaming progress log during download and extraction

### Players
- Real-time list of connected players via RCON
- **Kick individual player** button (moderator+)
- **Kick All** button (moderator+)

### Game Chat
- Live chat box showing player messages, joins, and leaves — parsed directly from `journalctl`
- Messages sent from the web UI appear in the chat box alongside in-game chat
- Date separators between messages from different days
- Auto-scrolls to the latest message; pauses auto-scroll if you scroll up
- Send messages to all players in-game from the web UI (user+)
- Polls every 2 seconds with deduplication

### Server Log
- Last 100 lines from the systemd journal, colour-coded by severity (Info / Warning / Error)
- Manual refresh button; auto-refreshes every 15 seconds

### Settings *(moderator / admin only)*
- **General**: server name, description, tags (shown in server browser), game join password (with show/hide toggle), max players
- **Visibility**: public server browser listing (off by default; warns if no account credentials are saved), LAN broadcast, require Factorio.com account
- **Behaviour**: auto-pause when empty, only admins can pause, AFK kick timeout, console commands (admins-only / all / nobody), research queue (always / after-victory / never)
- **Autosave**: interval (minutes), number of slots to keep, server-only autosave, non-blocking save (recommended for large maps)
- **In-game Admins**: list of Factorio usernames with in-game admin privileges (separate from web UI roles)
- Saving settings automatically restarts the server to apply them

### Save File Management *(moderator / admin only)*
- List all `.zip` save files with size, last-modified time, and the Factorio version they were last saved on
- Hover the version to see which mods (and their versions) were active at last save
- Upload a save file from your browser
- Download any save file directly to your machine
- Switch the active save (updates the service file and restarts)
- Delete saves (the active save is protected)
- **Generate New Map** — creates a fresh random world with a chosen name

### Factorio Account Credentials *(moderator / admin only)*
- Store your Factorio.com username and API token for downloading experimental builds
- Token is used only for the update endpoint; never exposed in the UI after saving

### RCON Configuration *(moderator / admin only)*
- Set the RCON port and password
- Automatically patches the systemd service file and reloads the daemon
- Restart the server once to activate

### Danger Zone *(admin only)*
- **Wipe Factorio** — stops the server, removes `/opt/factorio/` (including all saves), and deletes the systemd service file; use this before installing an older version

### User Management
Four permission tiers, ordered lowest to highest: **viewer → user → moderator → admin**

| Permission | Viewer | User | Moderator | Admin |
|---|:---:|:---:|:---:|:---:|
| View dashboard & logs | ✓ | ✓ | ✓ | ✓ |
| Start / Stop / Restart | | ✓ | ✓ | ✓ |
| Send chat messages | | ✓ | ✓ | ✓ |
| Kick players | | | ✓ | ✓ |
| Settings & saves | | | ✓ | ✓ |
| Manage user/viewer accounts | | | ✓ | ✓ |
| Manage all accounts | | | | ✓ |

- Moderators can add/delete/change-role of users with rank **below** them only
- Any user can change their own password
- First-run setup wizard creates the initial admin account

### Audit Logging
- Every significant action (login, start/stop/restart, kick, update, settings change, user management) is written to `/var/log/factorio-web/audit.log`
- Rotating log files: 1 MB per file, 10 files kept
- Each entry records timestamp, username, IP address, and action details

### First-Run Setup
- If no accounts exist, the app redirects to a setup wizard instead of the login page
- Creates the initial `admin` account; you log in normally after that

---

## Requirements

- Linux with **systemd**
- Python 3.10+
- Root access (the app controls systemd services and writes to `/opt/` and `/var/log/`)
- A Factorio.com account with an API token (needed to download the server binary)

## Installation

```bash
git clone https://github.com/youruser/factorio-server-manager.git
cd factorio-server-manager
sudo bash install.sh
```

The installer will:
1. Detect your distro and install Python dependencies via apt / dnf / yum / pacman / zypper
2. Ask for a port (default **8080**)
3. Copy files to `/opt/factorio-web/` and create a Python virtual environment
4. Register and start a `factorio-web` systemd service

Open `http://<your-server>:<port>` — the setup wizard will greet you on first launch.

### Upgrading

Run the installer again from the updated repo. It detects an existing installation and only overwrites `app.py` and `templates/` — your users, credentials, and settings are untouched.

```bash
git pull
sudo bash install.sh
```

### Manual / development setup

```bash
pip install -r requirements.txt gunicorn
gunicorn -w 1 -b 0.0.0.0:8080 app:app
```

> Use a single worker (`-w 1`) — the update progress and countdown state are in-process.

## File Layout

```
factorio-server-manager/
├── app.py                  # Flask application
├── install.sh              # Installer / upgrader
├── requirements.txt
├── static/
│   └── favicon.svg         # Browser tab icon
└── templates/
    ├── base.html           # Nav, CSS, shared layout
    ├── dashboard.html      # Main dashboard
    ├── settings.html       # Server settings + saves + RCON
    ├── users.html          # User management
    ├── login.html
    └── setup.html          # First-run wizard
```

Runtime files written outside the repo:

**Web manager** (`/opt/factorio-web/`)

| Path | Purpose |
|---|---|
| `/opt/factorio-web/app.py` | Installed application |
| `/opt/factorio-web/venv/` | Python virtual environment |
| `/opt/factorio-web/users.json` | User accounts (bcrypt-hashed passwords) |
| `/opt/factorio-web/factorio-credentials.json` | Factorio.com download credentials |
| `/opt/factorio-web/rcon.json` | RCON connection config |
| `/var/log/factorio-web/audit.log` | Rotating audit log (1 MB × 10 files) |
| `/etc/systemd/system/factorio-web.service` | Web UI service unit |

**Factorio game** (`/opt/factorio/`)

| Path | Purpose |
|---|---|
| `/opt/factorio/bin/x64/factorio` | Factorio server binary |
| `/opt/factorio/server-settings.json` | Server name, password, visibility, etc. |
| `/opt/factorio/saves/` | Save files (`.zip`) |
| `/opt/factorio/mods/mod-list.json` | Enabled mods — controls Space Age on/off |
| `/etc/systemd/system/factorio.service` | Factorio game service unit |
