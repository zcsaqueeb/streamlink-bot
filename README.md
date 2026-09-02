# 🎬 StreamLink Bot

**Transform Telegram media into instant, shareable streaming URLs with full admin control.**

![Python](https://img.shields.io/badge/language-Python-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)

## 📖 Overview

StreamLink Bot turns any file sent to your Telegram bot into a direct streaming or download link. It supports parallel transfers, batch link generation, live counters, and a robust admin suite—all built on Python, Docker, and Telethon.

## ✨ Features

- **Instant link generation** – upload a file and receive `/file`, `/stream`, and `/download` links within seconds.
- **Seek‑anywhere streaming** – custom player with HTTP Range support for smooth seeking.
- **Resumable downloads** – automatically resume from the last byte if a connection drops.
- **True parallel transfers** – each user and file download runs concurrently without queuing.
- **Batch links** – bundle multiple files behind one shareable page with search, filter, and download‑all functionality.
- **Live counters** – view, stream, and download statistics are shown per file in real time.
- **Admin suite** – statistics dashboard, bans, broadcasts, recent files, server status, and one‑tap restart.
- **White‑label branding** – configure site name, tagline, and bot identity from within Telegram.

## 🛠️ Tech Stack

- Python
- Docker
- Telethon (Telegram client library)
- Flask (web services)
- HTML/CSS (frontend)

## 📦 Installation

```bash
git clone https://github.com/zcsaqueeb/streamlink-bot.git
cd streamlink-bot
pip install -r requirements.txt
pip install .
docker build -t app .
```

## 🚀 Usage

```bash
python main.py
# or with Docker
docker run --rm app
```

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
├── owner_claim.py
├── requirements.txt
├── settings_store.py
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
├── plugins/maintenance.py
├── plugins/privacy.py
├── plugins/setup.py
├── plugins/start.py
├── plugins/stats.py
├── plugins/timezone.py
├── scripts/send_test_message.py
├── scripts/verify_web.py
```

## ⚙️ Configuration

Create a `.env` file from the example and set the required variables:

```bash
API_ID=your_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
```

All other settings (branding, limits, admin controls) are configured via the bot’s `/setup` command inside Telegram.

## 🤝 Contributing

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature-name`).
3. Commit your changes with clear messages.
4. Push to your fork and open a pull request.

## 📄 License

MIT License.