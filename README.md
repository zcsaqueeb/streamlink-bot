# 🎬 StreamLink Bot

**Transform Telegram media into instant, shareable streaming URLs with full admin control.**

![Python](https://img.shields.io/badge/language-Python-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)

## 📖 Overview

StreamLink Bot turns any file sent to your Telegram bot into a direct streaming or download link. It supports parallel transfers, batch link generation, live counters, and a robust admin suite—all built on Python, Docker, and Pyrogram.

## ✨ Features

- **Instant link generation** – upload a file and receive `/file`, `/stream`, and `/download` links within seconds.
- **Seek‑anywhere streaming** – custom player with HTTP Range support for smooth seeking.
- **Resumable downloads** – automatically resume from the last byte if a connection drops.
- **True parallel transfers** – each user and file download runs concurrently without queuing.
- **Batch links** – bundle multiple files behind one shareable page with search, filter, and download‑all functionality.
- **Subtitles & audio‑language switching** – videos with embedded captions or multiple audio streams get a real switcher in the web player (via an off‑request FFmpeg pipeline; degrades gracefully when FFmpeg isn't installed).
- **`/sub` – attach subtitles later** – send a `.srt`, `.ass` or `.vtt` file to a link you already made; it's converted to WebVTT on the spot and attached without minting a new URL, so every copy of the old link keeps working. `/unsub` takes one back off.
- **Link management** – `/mylinks` lists the links you've generated, `/rename` fixes a filename while the link itself stays live, and `/unlink` lets the uploader take a link back down without waiting for an admin.
- **Optional public gallery** – with `public_gallery` enabled, `/latest` lists the most recent uploads and the landing page shows a "Recently shared" strip. Off by default, and always kept out of search engines.
- **Password‑protected links** – `/lock <file ID>` puts a password on any link you own and `/unlock` takes it back off. Until the password is right, the page, the player, the raw bytes and even the file's name are all withheld; unlocked browsers get a signed cookie that lasts a week, and repeated wrong guesses lock just that link for a while.
- **Live counters** – view, stream, and download statistics are shown per file in real time.
- **Admin suite** – statistics dashboard, bans, broadcasts, recent files, server status, and one‑tap restart.
- **White‑label branding** – configure site name, tagline, and bot identity from within Telegram.

## 🛠️ Tech Stack

- Python (Pyrogram for the Telegram MTProto client)
- aiohttp + Jinja2 (web server and templating)
- MongoDB (Motor) or a JSON file store
- Docker
- FFmpeg (optional — powers subtitle/audio-track extraction)

## 📦 Installation

```bash
git clone https://github.com/zcsaqueeb/streamlink-bot.git
cd streamlink-bot
pip install -r requirements.txt
docker build -t app .
```

This is an application, not an installable package — there is no `setup.py` or
`pyproject.toml`, so `pip install .` will fail. Install the dependencies and run
`bot.py` from the project directory.

## 🚀 Usage

```bash
python bot.py
# or with Docker — the three credentials have to be passed in, otherwise
# startup stops with a configuration error
docker run --rm -p 8080:8080 \
  -e BOT_TOKEN=... -e API_ID=... -e API_HASH=... \
  -v ./data:/app/data \
  app
```

Mounting `/app/data` (or any host directory) keeps `bot_settings.json`,
`local_db.json` and the session file across container rebuilds — see
`LOCAL_DB_PATH` and `SETTINGS_PATH` in `.env.example`.

## 📂 Project Structure

```text
├── __pycache__/
├── database/
├── plugins/
├── scripts/
├── web/
├── .env.example
├── Dockerfile
├── Procfile
├── README.md
├── batch_state.py
├── bot.py
├── info.py
├── lock_state.py
├── owner_claim.py
├── requirements.txt
├── settings_store.py
├── sub_state.py
├── transfer_stats.py
├── utils.py
├── database/__init__.py
├── database/database.py
├── plugins/__init__.py
├── plugins/admin.py
├── plugins/antispam.py
├── plugins/auto_help.py
├── plugins/batch.py
├── plugins/caption.py
├── plugins/file_handler.py
├── plugins/links.py
├── plugins/lock.py
├── plugins/maintenance.py
├── plugins/privacy.py
├── plugins/setup.py
├── plugins/start.py
├── plugins/stats.py
├── plugins/subtitles.py
├── plugins/timezone.py
├── scripts/send_test_message.py
├── scripts/verify_db.py
├── scripts/verify_web.py
├── web/__init__.py
├── web/app.py
├── web/cache.py
├── web/locks.py
├── web/media.py
├── web/render.py
├── web/security.py
```

## ⚙️ Configuration

Create a `.env` file from the example and set the required variables:

```bash
API_ID=your_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
```

All other settings (branding, limits, admin controls) are configured via the bot’s `/setup` command inside Telegram.

Those go to `bot_settings.json`, which is git‑ignored because it also holds the
signing secret for `/lock` unlock cookies (`cookie_secret`, generated on first
web start). Treat that file like `.env`: keep it private, and don't paste it
into a bug report. If it's ever lost, nothing breaks — existing browsers are
simply asked for the password again, and a fresh secret is generated.

If the web server sits behind a proxy, `TRUSTED_PROXY_CIDRS` says which
addresses may be trusted with `X-Forwarded-For`. Loopback and private ranges
already are, which covers nginx/Caddy on the same host and container-network
ingress. A proxy at a public IP (Cloudflare talking to aiohttp directly, for
example) must be listed there, or every visitor is bucketed under the proxy's
address and shares one rate limit — and the `/lock` guess cap stops meaning
anything.

## 💬 Commands

Everything is driven from inside Telegram. Non-admin users only ever see the
first group; admins get the rest added to their own menu.

**Everyone**

| Command | What it does |
| --- | --- |
| `/start`, `/help`, `/about` | Welcome message, how-to, bot info |
| send a file | Makes the link — no command needed |
| `/batch`, `/done`, `/mybatch`, `/cancel` | Bundle several files behind one page |
| `/mylinks` | Every link you've generated, newest first |
| `/rename <id> <name>` | Fix a filename; the link stays the same |
| `/unlink <id>` | Take one of your own links down |
| `/sub <id> [Label]` | Attach a `.srt`/`.ass`/`.vtt` to a video you already shared |
| `/unsub <id> <index>` | Detach an attached subtitle |
| `/lock <id>`, `/unlock <id>` | Put a password on a link, or remove it |
| `/status`, `/ping`, `/id` | Your account status, bot responsiveness, your user ID |
| `/privacy` | See and delete what this bot stores about you |
| `/timezone` | Timezone used on dates and times |

**Admins**

`/settings`, `/setup`, `/restart`, `/maintenance`, `/stats`, `/analytics`,
`/serverstatus`, `/users`, `/userinfo`, `/recentfiles`, `/broadcast`, `/ban`,
`/unban`, `/unwarn`, `/delete`, `/deleteall`, `/confirmdeleteall`,
`/addadmin`, `/admins`, `/setcaption`, `/getcaption`, `/resetcaption`

## 🤝 Contributing

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature-name`).
3. Commit your changes with clear messages.
4. Push to your fork and open a pull request.

## 📄 License

MIT License.