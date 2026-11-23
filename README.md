<div align="center">

# 🎬 StreamLink Bot

**Turn any Telegram file into a private download link, a seek-anywhere stream and a shareable web page — configured entirely from inside Telegram.**

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![Pyrogram](https://img.shields.io/badge/Pyrogram-2.0-informational) ![Docker](https://img.shields.io/badge/docker-ready-2496ED) ![License](https://img.shields.io/badge/license-MIT-green) ![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)

[Quick start](#-quick-start) · [Configuration](#-configuration) · [Commands](#-commands) · [Web portal](#-web-portal) · [Deployment](#-deployment-guide) · [Troubleshooting](#-troubleshooting)

</div>

---

## 📑 Table of contents

1. [Overview](#-overview)
2. [Features](#-features)
3. [How it works](#-how-it-works)
4. [Requirements](#-requirements)
5. [Quick start](#-quick-start)
6. [First-run setup](#-first-run-setup-in-telegram)
7. [Configuration](#-configuration)
8. [Using the bot](#-using-the-bot)
9. [Commands](#-commands)
10. [Web portal](#-web-portal)
11. [Security & privacy](#-security--privacy)
12. [Deployment guide](#-deployment-guide)
13. [Operations](#-operations)
14. [Project structure](#-project-structure)
15. [Development & testing](#-development--testing)
16. [Troubleshooting](#-troubleshooting)
17. [FAQ](#-faq)
18. [Contributing](#-contributing) · [License](#-license)

---

## 📖 Overview

StreamLink Bot sits between Telegram and a small web server. Send a file to the bot and it:

1. stores the file in a **private storage channel you control**,
2. replies with a **download link**, a **stream link** (for video/audio) and a **web page**,
3. serves those links straight from Telegram with **HTTP Range support**, so videos seek instantly and downloads resume.

Nothing is stored on the web server itself — it only generates the door to reach the file. Everything about the bot (storage channel, branding, link lifetime, limits) is set up **from the Telegram chat**; the only things in `.env` are the three Telegram credentials.

---

## ✨ Features

**Links & sharing**

- ⚡ **Instant links** — download, stream and web-page URLs within seconds of upload.
- 🔗 **`/link`** — get any link (and its buttons) back at any time; **`/mylinks`** lists everything you've made.
- 📦 **Batch links** — bundle many files behind one page with search, filter, *Download all* and an `.m3u` playlist.
- ⏳ **Expiry** — a server-wide default plus per-link `/expire` (1 minute – 365 days).
- 🔒 **Password-protected links** — `/lock` hides the page, player, bytes and even the filename until the password is right.
- ✏️ **Rename / take down** — `/rename` keeps the link alive, `/unlink` removes it.

**Streaming & playback**

- ▶️ **Custom web player** — seek-anywhere, speed, loop, Picture-in-Picture, fullscreen, AirPlay, resume-where-you-left-off, double-tap ±10 s, keyboard shortcuts.
- 💬 **Subtitles & audio languages** — embedded tracks are extracted with FFmpeg; `/sub` attaches a `.srt`/`.ass`/`.vtt` later without changing the link.
- 📺 **Play in your own player** — one-tap VLC / MX Player buttons and `.m3u` playlists for VLC, mpv and Kodi.
- ⏬ **Resumable downloads** — real progress, automatic resume after a dropped connection.
- 🖼️ **Thumbnails**, light/dark/system theme, fully responsive, accessible (keyboard, screen readers, reduced-motion).

**Administration**

- 🧭 **In-chat setup wizard** and live `/settings` — no config files to edit.
- 📊 `/stats`, `/analytics` (charts), `/serverstatus`, `/users`, `/recentfiles`.
- 📣 `/broadcast`, `/ban`/`/unban`, built-in anti-spam with temporary bans, `/maintenance` mode.
- 🎨 Custom storage-channel captions, optional uploader info, optional public `/latest` gallery.
- 🏷️ **White-label** — site name, tagline, bot name and creator name are all configurable.
- 🗄️ **MongoDB** for production, or a zero-setup JSON store for small deployments.

---

## 🧩 How it works

```text
 You ──send file──▶ Telegram bot ──copy──▶ Private storage channel (DB channel)
                         │                          ▲
                         │ link + buttons            │ streamed on demand
                         ▼                          │
                      You / friends ──open link──▶ Web server (aiohttp) ──▶ Browser, VLC, download manager
                                                       │
                                                       └─ metadata: MongoDB or local JSON
```

| Stage | What happens |
| --- | --- |
| **Uplink** | The bot receives your file and copies it (by reference, no re-upload) into the DB channel. |
| **Relay** | It records the file's metadata and mints an unguessable 12-character ID. |
| **Downlink** | `/stream/<id>` and `/download/<id>` fetch bytes from Telegram on demand and honour `Range` requests. |

Links are private by being **unguessable**, not by being hidden — treat a link like a key, and use `/lock` when you need a real password.

---

## ✅ Requirements

| Need | Notes |
| --- | --- |
| **Python 3.10+** (3.11 recommended) | Or just use Docker. |
| **Telegram credentials** | `API_ID` + `API_HASH` from <https://my.telegram.org/apps>, `BOT_TOKEN` from [@BotFather](https://t.me/BotFather). |
| **A private channel** | The bot must be an **admin** there; it's the file store. |
| **A public URL** *(for web links)* | Any host that exposes the port (Railway, Render, a VPS + domain…). |
| **MongoDB** *(recommended)* | Free Atlas tier is plenty. Without it, a JSON file is used. |
| **FFmpeg** *(optional)* | Enables subtitle/audio-track extraction. Included in the Docker image. |
| **libmagic** *(optional)* | Better file-type detection. Included in the Docker image. |

---

## 🚀 Quick start

### Option A — Docker (recommended)

```bash
git clone https://github.com/zcsaqueeb/streamlink-bot.git
cd streamlink-bot
cp .env.example .env          # then fill in BOT_TOKEN, API_ID, API_HASH

docker build -t streamlink-bot .
docker run -d --name streamlink --restart unless-stopped \
  --env-file .env \
  -p 8080:8080 \
  -e LOCAL_DB_PATH=/app/data/local_db.json \
  -e SETTINGS_PATH=/app/data/bot_settings.json \
  -v "$(pwd)/data:/app/data" \
  streamlink-bot

docker logs -f streamlink     # the first-run claim code appears here
```

The two `-e` paths plus the volume make your **settings and links survive rebuilds** (see [Persistence](#persistence)).

### Option B — Run directly

```bash
git clone https://github.com/zcsaqueeb/streamlink-bot.git
cd streamlink-bot
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # fill in the three credentials
python bot.py
```

> This is an application, not an installable package — there is no `setup.py`/`pyproject.toml`, so `pip install .` will not work. Install the requirements and run `bot.py` from the project folder.

### Option C — Platform as a service

Railway, Render, Heroku and similar hosts work out of the box: a `Dockerfile` and a `Procfile` (`web: python -u bot.py`) are included. Set `BOT_TOKEN`, `API_ID` and `API_HASH` as environment variables, **attach a persistent volume** (or set `MONGO_URI` later from the bot), and deploy. See the [deployment guide](#-deployment-guide).

---

## 🧭 First-run setup (in Telegram)

On a brand-new install nobody is an admin yet, so the bot protects the claim:

1. Start the bot and watch its **console / logs**. A **6-digit claim code** is printed and **rotates every 2 minutes**.
2. Open the bot in Telegram, send `/start`, and reply with the current code. You become the **owner/admin**.
3. The **setup wizard** walks you through the settings below. Only the first is required — tap **Skip** on the rest.

| Step | Setting | Required | What it does |
| --- | --- | :---: | --- |
| 📥 | **DB Channel** | ✅ | Private channel (bot = admin) where files are stored. Send its **@username**, a **t.me link** (public, invite or post link), its ID (`-100…`), or forward a message from it. |
| 📝 | Log Channel | | Receives startup/operational notices. Same input options as the DB channel. |
| 🔒 | Force-Sub Channel | | Users must join this channel before using the bot (bot must be admin). Same input options. |
| 🗄️ | MongoDB URI | | Recommended for production; otherwise a local JSON store is used. |
| 🌐 | **Website URL** | | Public address of your deployment. **Setting it turns the web portal on** and adds links to every upload. |
| 📛 🏷️ | Site name / tagline | | Branding shown on the website. |
| 🤖 👤 | Bot name / creator name | | Shown in the bot and on pages. |
| ⚡ | Parallel transfers | | Simultaneous Telegram transfers (default 200). |
| ▶️ | Stream mode | | Enable in-browser streaming. |
| ⏳ | Link expiry | | Default lifetime in days; empty = permanent. |

**Channel inputs are checked for you:** when you send a username or link, the bot looks the channel up and confirms it is a channel and that it is an admin there, telling you exactly what to fix if not.

Re-run the wizard any time with `/setup`, or change individual values with `/settings`. Some settings apply instantly; the rest trigger an automatic restart.

> **No admin yet and no console access?** The code is only ever shown on the server's console/logs — that's deliberate, so a stranger can't claim an unconfigured bot just by messaging it first.

---

## 🔧 Configuration

### Environment variables (`.env`)

| Variable | Required | Default | Purpose |
| --- | :---: | --- | --- |
| `BOT_TOKEN` | ✅ | — | Bot token from @BotFather. |
| `API_ID` | ✅ | — | Numeric ID from my.telegram.org. |
| `API_HASH` | ✅ | — | Hash from my.telegram.org. |
| `PORT` | | `8080` | Port the web server listens on (hosts usually inject this). |
| `WEB_SERVER_BIND_ADDRESS` | | `0.0.0.0` | Interface to bind. |
| `BOT_USERNAME` | | auto | Override the detected bot username. |
| `LOCAL_DB_PATH` | | `local_db.json` | JSON store used when there's no MongoDB. |
| `SETTINGS_PATH` | | `bot_settings.json` | Where in-chat settings are saved. |
| `TG_BOT_WORKERS` | | `32` | Telegram updates handled concurrently. |
| `TRUSTED_PROXY_COUNT` | | `1` | Reverse-proxy hops in front of the server (counted from the right of `X-Forwarded-For`). |
| `TRUSTED_PROXY_CIDRS` | | loopback + private | Extra proxy addresses allowed to set `X-Forwarded-For`. |
| `MEDIA_CACHE_DIR` | | `media_cache` | Where extracted subtitle/audio tracks are cached. |
| `MEDIA_TRACK_MAX_GB` | | `4` | Skip track extraction for files larger than this. |
| `NO_COLOR` | | — | Disable colour in the startup banner. |

Everything else lives in the chat (below) and is saved to `bot_settings.json`.

### Settings inside Telegram (`/settings`)

| Setting | Effect | Applies |
| --- | --- | --- |
| DB / Log / Force-sub channels | Storage, notices, join gate | after restart |
| MongoDB URI, Website URL, Parallel transfers | Infrastructure | after restart |
| Site name, tagline, bot name, creator | Branding | after restart |
| Stream mode, Link expiry | Link behaviour | after restart |
| 👤 Show uploader info | Adds an “Uploaded by” block to storage captions | **live** |
| 🗑️ Auto-delete messages | Deletes the upload + reply after N seconds | **live** |
| 🎧 Subtitle & audio tracks | Turns FFmpeg extraction on/off | **live** |
| 🌐 Public latest page | Enables the `/latest` gallery and landing-page strip | **live** |
| Maintenance mode (`/maintenance`) | Pauses new uploads for non-admins | **live** |
| Caption template (`/setcaption`, `/captions`) | Storage-channel caption format | **live** |

### ⚠️ Keep `bot_settings.json` private

It holds the admin list, channel IDs and the **cookie-signing secret** for `/lock`. It's git-ignored and excluded from Docker builds — treat it like `.env` and never paste it into a bug report. If it's lost nothing breaks: browsers are simply asked for passwords again and a new secret is generated.

---

## 📲 Using the bot

**Make a link** — just send a file (document, video, audio, voice note, GIF, photo, sticker). You get a card like:

```text
✅ Link generated!

📁 Movie.2024.1080p.mkv
💾 1.40 GB · 📂 video/x-matroska
♾️ Expiry: never (permanent)
🆔 ID: 4c06e7b21fb5

⬇️ Download: https://your.site/download/4c06e7b21fb5
▶️ Stream:   https://your.site/stream/4c06e7b21fb5
🌐 Page:     https://your.site/file/4c06e7b21fb5
```

…with **Download · Stream · Open web page · Share** buttons. Tap any link to copy it.

**Bundle files** — `/batch`, send as many files as you like, then `/done` to get one page for all of them (`/mybatch` to check, `/cancel` to abort).

**Come back later** — `/link` re-sends your latest card, `/link <ID>` any other, `/mylinks` lists them all, `/search <text>` finds one by name.

**Manage a link** (use the ID from the card; you can also paste the whole link instead of the ID):

```text
/rename 4c06e7b21fb5 Better Name.mkv     # fix the name, link keeps working
/expire 4c06e7b21fb5 7d                  # 30m · 12h · 7d · 2w · 1d12h · never
/lock   4c06e7b21fb5                     # then send the password as a normal message
/unlock 4c06e7b21fb5
/sub    4c06e7b21fb5 English             # then send a .srt / .ass / .vtt file
/unsub  4c06e7b21fb5 [track]
/unlink 4c06e7b21fb5                     # take it down for good
```

> `/lock` asks for the password in a **separate message** (never as an argument) and deletes both messages once it's read, so the password doesn't sit in your chat history.

---

## 💬 Commands

Non-admins see the first table in their “/” menu; admins get both.

### Everyone

| Command | Description |
| --- | --- |
| `/start` · `/help` · `/about` | Welcome, how-to, bot info |
| *(send a file)* | Generates the link — no command needed |
| `/link [ID]` · `/getlink` | Re-send a link card with buttons (latest upload if no ID) |
| `/mylinks [page]` | Your links, 10 per page, with IDs |
| `/search <text>` | Find one of your links by file name |
| `/rename <ID> <name>` | Rename a file; the link stays the same |
| `/expire <ID> <time\|never>` | Set when a link stops working |
| `/lock <ID>` · `/unlock <ID>` | Add or remove a password |
| `/unlink <ID>` | Take one of your links down |
| `/sub <ID> [label]` · `/unsub <ID> [track]` | Attach / detach a subtitle on a video |
| `/batch` · `/mybatch` · `/done` · `/cancel` | Bundle several files into one link |
| `/mystats` | Your uploads and how much they're used |
| `/status` · `/ping` · `/id` | Account status, responsiveness, your Telegram ID |
| `/privacy [on\|off]` | Hide your identity in upload captions (display only) |
| `/mydata` | See what the bot has stored about you |
| `/timezone` | Show dates and times in your timezone |

### Admins

| Area | Commands |
| --- | --- |
| **Setup** | `/setup`, `/settings`, `/restart`, `/maintenance` |
| **Insight** | `/stats`, `/analytics`, `/serverstatus`, `/users`, `/userinfo`, `/recentfiles`, `/admins` |
| **Moderation** | `/ban`, `/unban`, `/unwarn`, `/broadcast` |
| **Content** | `/delete <ID>` *(asks to confirm)*, `/deleteall` + `/confirmdeleteall` *(60-second window)* |
| **People** | `/addadmin` |
| **Captions** | `/captions`, `/setcaption`, `/getcaption`, `/resetcaption` |

Admins can also run `/link`, `/rename`, `/expire`, `/unlink` etc. on **anyone's** files.

**Anti-spam:** sending messages too fast earns warnings; after **3 warnings** a user gets a **15-minute temporary ban**. Admins are exempt, and users mid-batch aren't rate-limited.

---

## 🌐 Web portal

The portal switches on as soon as a **Website URL** is configured.

| Route | Purpose |
| --- | --- |
| `/` | Landing page |
| `/file/<id>` | File page: details, player, buttons |
| `/stream/<id>` | Raw stream (Range-enabled) — open in VLC, mpv, etc. |
| `/download/<id>` | Raw download (resumable) |
| `/playlist/<id>` | One-file `.m3u` playlist |
| `/batch/<id>` | Batch page; `/batch/<id>/playlist.m3u` queues every playable file |
| `/thumbnail/<id>` | Video/photo thumbnail |
| `/latest` | Public gallery of recent uploads *(off by default, never indexed)* |
| `/info/<id>` | JSON metadata (blocked for locked files) |
| `/health` | Lightweight liveness check |

### Player features & shortcuts

| Key | Action | Key | Action |
| --- | --- | --- | --- |
| `Space` / `K` | Play / pause | `M` | Mute |
| `←` / `→` | Seek −10 s / +10 s | `F` | Fullscreen |
| `↑` / `↓` | Volume | `P` | Picture-in-Picture |
| `C` | Cycle subtitles | `L` | Toggle loop |

On touch screens: **double-tap** the left/right side to skip 10 s, **swipe** to seek. Volume, speed, loop and each file's chosen subtitle/audio language are remembered. Browser shortcuts (Ctrl/Cmd + F, P, …) are never hijacked.

### Formats a browser can't play

Some containers/codecs (many MKVs, HEVC) aren't decodable by browsers. The page then offers **Play in your own player**: VLC, MX Player (Android) or an `.m3u` playlist for VLC/mpv/Kodi — or just paste the `/stream/<id>` URL into any media player.

---

## 🔐 Security & privacy

- **Unguessable IDs** — links are random 12-character tokens; there's no listing unless you turn `/latest` on.
- **Password links** — PBKDF2-hashed, checked off the event loop, delivered with a signed cookie valid for a week; wrong guesses are throttled to 8 per 15 minutes per visitor. Re-locking signs everyone out.
- **Locked means locked** — page, player, bytes, thumbnails, subtitles, `/info` and even the filename are withheld. Opening a locked file through the bot's `/start <id>` link is refused too, unless you're the uploader or an admin.
- **Hardened responses** — CSP, `X-Frame-Options: DENY`, `nosniff`, per-IP rate limiting (120 requests/minute on page, batch, info and track routes).
- **Uploaded files can't run on your site** — only video, audio and common images are shown inline. HTML, SVG, XML, PDF and anything unknown is forced to download as opaque bytes with `Content-Security-Policy: sandbox`, so an uploaded page can never execute script on the portal (no stored XSS).
- **Malware blocklist** — executables and scripts (`exe`, `bat`, `scr`, `ps1`, `vbs`, `jar`, `apk`, `msi`, `lnk`, `sh`, macro Office files and more) are refused at upload, including double extensions like `invoice.pdf.exe`. Admins are exempt. Change it with `BLOCKED_EXTENSIONS=ext1,ext2` or `BLOCKED_EXTENSIONS=none`.
- **Safe media parsing** — ffmpeg/ffprobe only read the local file and only real media containers, so a crafted playlist can't read server files or call internal URLs.
- **Least privilege** — the Docker image runs as a non-root user. If you mount a volume, make it writable by UID `10001`.
- **No SQL/NoSQL injection surface** — user input never builds queries or shell commands; templates auto-escape, and bot messages escape Markdown.
- **Honest limits** — no system is unhackable. The bot does **not** scan file contents for viruses; for that, put ClamAV or a scanning proxy in front. Keep dependencies updated (`pip-audit`) and use a strong password on locked links.
- **Ownership checks** — users can only act on their own files; admins can act on any.
- **Secrets** — `.env` and `bot_settings.json` are git-ignored and kept out of Docker images.
- **Gallery is opt-in** — `/latest` is off by default, `noindex`, and blocked in `robots.txt`.
- **`/privacy`** only hides your name/username/ID in the *caption text*; admins can always see the real uploader. It's a display toggle, not anonymity.

---

## 🚢 Deployment guide

### Persistence

| Data | Where | Keep it by |
| --- | --- | --- |
| Settings, admins, cookie secret | `SETTINGS_PATH` (`bot_settings.json`) | Mounting a volume **or** relying on re-running `/setup` |
| Links / users (no MongoDB) | `LOCAL_DB_PATH` (`local_db.json`) | Mounting a volume — **otherwise links break on redeploy** |
| Links / users (MongoDB) | MongoDB | Nothing to do — recommended |
| Subtitle/audio cache | `MEDIA_CACHE_DIR` | Optional; re-extracted on demand |

The Telegram session is kept **in memory only**, so there is no session file to persist or corrupt. **Use MongoDB for anything permanent.**

### Railway / Render / similar

1. Deploy the repo (Dockerfile is detected).
2. Add `BOT_TOKEN`, `API_ID`, `API_HASH`.
3. Add a persistent volume, or plan to set a **MongoDB URI** from `/setup`.
4. Open the service logs, read the **claim code**, message the bot, finish `/setup`.
5. Set **Website URL** to the service's public address and let the bot restart.

### VPS behind a reverse proxy

```nginx
server {
    server_name files.example.com;
    client_max_body_size 0;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;           # lets large streams start immediately
        proxy_read_timeout 3600s;
    }
}
```

Get HTTPS with Certbot or Caddy. With **Cloudflare → nginx → app** set `TRUSTED_PROXY_COUNT=2`; a proxy at a *public* IP talking to the app directly must be listed in `TRUSTED_PROXY_CIDRS`, or every visitor shares one rate-limit bucket.

### Docker Compose

```yaml
services:
  streamlink:
    build: .
    restart: unless-stopped
    env_file: .env
    environment:
      LOCAL_DB_PATH: /app/data/local_db.json
      SETTINGS_PATH: /app/data/bot_settings.json
    ports: ["8080:8080"]
    volumes: ["./data:/app/data"]
```

---

## 🧰 Operations

- **Health:** `GET /health` returns `200`; the Docker image has a built-in `HEALTHCHECK`.
- **Restart:** `/restart` (asks for confirmation) re-launches the process and keeps all data.
- **Maintenance:** `/maintenance` pauses new uploads for non-admins; existing links keep working.
- **Stats:** `/stats`, `/analytics` (charts, needs `matplotlib`), `/serverstatus`.
- **Backups:** back up MongoDB (or `local_db.json`) and `bot_settings.json`.
- **Cleanup:** expired links are removed automatically on an hourly sweep.
- **Logs:** everything goes to stdout; the startup card is also sent to admins and the log channel.

---

## 📂 Project structure

```text
bot.py                  entry point — Pyrogram client + web server
info.py                 config loader (env + in-chat settings)
settings_store.py       persistent in-chat settings
utils.py                shared helpers (captions, expiry, locks, force-sub…)
database/database.py    MongoDB / local-JSON storage layer
plugins/                Telegram commands & handlers
  file_handler.py       upload → link card, /delete
  links.py              /link /mylinks /rename /unlink
  myfiles.py            /search /expire /mystats /mydata
  batch.py              /batch /done /mybatch /cancel
  lock.py  subtitles.py /lock /unlock · /sub /unsub
  start.py  auto_help.py  /start /help /about · friendly fallbacks
  setup.py  admin.py      setup wizard & settings · admin tools
  antispam.py  maintenance.py  stats.py  caption*.py  privacy.py  timezone.py
web/                    aiohttp web portal
  app.py                routes (pages, stream, download, playlists)
  media.py              FFmpeg subtitle/audio extraction
  locks.py  security.py password cookies · headers & rate limiting
  cache.py  render.py   Telegram stream cache · Jinja setup
  templates/  static/   pages, theme CSS/JS
scripts/                offline verification scripts
Dockerfile  Procfile  requirements.txt  .env.example
```

---

## 🧪 Development & testing

```bash
pip install -r requirements.txt
python scripts/verify_db.py     # storage layer checks (no Telegram, no network)
python scripts/verify_web.py    # in-process web-layer smoke tests (needs aiohttp, jinja2)
python -m py_compile bot.py plugins/*.py web/*.py database/*.py   # quick syntax check
```

`scripts/send_test_message.py` sends a test message with your credentials. Both verification scripts stub Telegram and MongoDB, so they run anywhere. Keep code compatible with **Python 3.11** (the Docker image); avoid 3.12-only syntax such as reusing the same quote inside an f-string expression.

---

## 🩺 Troubleshooting

| Symptom | Likely cause & fix |
| --- | --- |
| Bot exits: *“CONFIGURATION ERROR”* | `.env` still has placeholders. Set real `BOT_TOKEN`, numeric `API_ID`, `API_HASH`. |
| “Bot not configured” on upload | No **DB Channel** yet — run `/setup`. |
| “Storage Error” on upload | The bot isn't **admin** in the DB channel, or the ID is wrong (`-100…`). |
| Can't see the claim code | It prints to the server console/logs only. Check `docker logs` or your host's log viewer. |
| No web links in replies | **Website URL** isn't set. Set it in `/settings`. |
| All old links return 404 after a redeploy | Ephemeral disk with no MongoDB. Add a volume for `LOCAL_DB_PATH` or set a **MongoDB URI**. |
| Video won't play in the browser | Unsupported codec/container. Use **Play in your own player** (VLC) or the `.m3u`. |
| No subtitle/audio switcher | FFmpeg missing, extraction turned off, or the file has a single stream. Tap **Load** under player settings. |
| Everyone is rate-limited at once | Proxy not trusted — set `TRUSTED_PROXY_COUNT` / `TRUSTED_PROXY_CIDRS`. |
| `FloodWait` in logs | Telegram throttling a burst of uploads; the bot waits it out automatically. |
| Users locked out after sending quickly | Anti-spam temp ban (15 min). Admins can `/unwarn` or `/unban`. |
| `AUTH_BYTES_INVALID` | Fixed by design — the session is in-memory; just restart. |

---

## ❓ FAQ

**Does the web server store my files?** No. It streams them from Telegram on demand; the storage channel is the only copy.

**Is there a file-size limit?** Whatever Telegram allows for your bot's uploads. Browser downloads over ~512 MB are handed to the browser's own download manager instead of being buffered in memory.

**Can I run it without a website?** Yes — leave **Website URL** empty and the bot still stores files, but you get no web links.

**Can I change the branding?** Yes: site name, tagline, bot name and creator name are all in `/settings`.

**How do I add another admin?** `/addadmin` (admins only).

**Will a link keep working if I rename the file?** Yes — `/rename` only changes the name.

---

## 🤝 Contributing

1. Fork the repository.
2. Create a branch (`git checkout -b feature/my-change`).
3. Make your change and run the [verification scripts](#-development--testing).
4. Commit with a clear message, push, and open a pull request.

Bug reports are welcome — include logs, but **redact tokens and `bot_settings.json`**.

## 📄 License

Released under the **MIT License**.
