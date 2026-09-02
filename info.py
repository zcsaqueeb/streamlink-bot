"""
Configuration — reads from environment variables (populated by .env via python-dotenv).
"""

import os
import sys
from typing import List

import settings_store

# ── Required ──────────────────────────────────────────────────────────────────
BOT_TOKEN: str  = os.environ.get("BOT_TOKEN", "").strip()
_raw_api_id: str = os.environ.get("API_ID", "").strip()
API_HASH: str   = os.environ.get("API_HASH", "").strip()

# ── Friendly validation ──────────────────────────────────────────────────────
# Catches the common mistake of leaving placeholder values (e.g.
# "your_api_id_here") in .env, which would otherwise crash with a confusing
# ValueError traceback on int(API_ID).
_missing = []
if not BOT_TOKEN or BOT_TOKEN.lower().startswith("your_"):
    _missing.append("BOT_TOKEN")
if not _raw_api_id or not _raw_api_id.isdigit():
    _missing.append("API_ID")
if not API_HASH or API_HASH.lower().startswith("your_"):
    _missing.append("API_HASH")

if _missing:
    print(
        "\n"
        "==============================================================\n"
        " CONFIGURATION ERROR — your .env file is not set up correctly\n"
        "==============================================================\n"
        f" Missing or placeholder value(s): {', '.join(_missing)}\n\n"
        " Open your .env file and fill these in with real values:\n"
        "   BOT_TOKEN  -> from @BotFather on Telegram\n"
        "   API_ID     -> a NUMBER from https://my.telegram.org/apps\n"
        "   API_HASH   -> a string from https://my.telegram.org/apps\n\n"
        " Example:\n"
        "   BOT_TOKEN=123456789:AAExampleBotTokenFromBotFather\n"
        "   API_ID=12345678\n"
        "   API_HASH=abcdef1234567890abcdef1234567890\n"
        "==============================================================\n",
        file=sys.stderr,
    )
    sys.exit(1)

API_ID: int = int(_raw_api_id)

# ── Channel IDs ───────────────────────────────────────────────────────────────
# NOTE: LOG_CHANNEL / DB_CHANNEL / FORCE_SUB_CHANNEL are no longer read from
# .env. They're configured by the admin from inside Telegram the first time
# the bot runs (see plugins/setup.py) and persisted via settings_store.
def _int_env(key: str, default: int = 0) -> int:
    parts = os.environ.get(key, "").strip().split()
    try:
        return int(parts[0]) if parts else default
    except (ValueError, IndexError):
        return default

LOG_CHANNEL: int        = int(settings_store.get("log_channel", 0) or 0)
DB_CHANNEL: int         = int(settings_store.get("db_channel", 0) or 0)
FORCE_SUB_CHANNEL: int  = int(settings_store.get("force_sub_channel", 0) or 0)

# ── Admins ────────────────────────────────────────────────────────────────────
# NOTE: no longer read from .env — configured via Telegram (see plugins/setup.py).
# Kept as a genuine mutable list object: other modules do
# `from info import ADMINS` and then mutate it in place (ADMINS.append(...)),
# so every module that imported it shares live updates without a restart.
ADMINS: List[int] = [int(x) for x in (settings_store.get("admins", []) or [])]

# ── Owner-claim protection ────────────────────────────────────────────────────
# The first-run owner claim is protected by a rotating 6-digit code printed
# to the console every 2 minutes — see owner_claim.py and
# plugins/setup.py maybe_claim_owner(). The first person to message an
# unconfigured bot must read that code off the live console/log output and
# send it back before being promoted to admin, so a stranger can't win the
# old "whoever messages first becomes admin" race without actually watching
# the running process.

# ── Bot identity ──────────────────────────────────────────────────────────────
# NOTE: no longer read from .env — configured via Telegram (see plugins/setup.py).
BOT_NAME: str     = (settings_store.get("bot_name") or "").strip() or "File-to-Link Bot"
BOT_USERNAME: str = os.environ.get("BOT_USERNAME", "").strip()

# ── Web platform identity ─────────────────────────────────────────────────────
# This is a LINK-GENERATOR + STREAMING platform, not a file-storage service.
# Branding shown across the website. NOTE: no longer read from .env —
# configured via Telegram (see plugins/setup.py) and persisted via settings_store.
SITE_NAME: str    = (settings_store.get("site_name") or "").strip() or "StreamLink"
SITE_TAGLINE: str = (
    (settings_store.get("site_tagline") or "").strip()
    or "Generate instant links & stream anything — right in your browser."
)
# NOTE: no longer read from .env — configured via Telegram (see plugins/setup.py).
CREATOR_NAME: str = (settings_store.get("creator_name") or "").strip() or "Saqueeb"

# ── Web server ────────────────────────────────────────────────────────────────
# NOTE: WEB_SERVER_BIND_ADDRESS / PORT stay in .env — those are deployment
# infra (which interface/port the process listens on), decided by the host,
# not something to configure from inside a chat.
WEB_SERVER_BIND_ADDRESS  = os.environ.get("WEB_SERVER_BIND_ADDRESS", "0.0.0.0")
WEB_SERVER_PORT: int     = _int_env("PORT", 8080)

# ── Link settings ─────────────────────────────────────────────────────────────
# NOTE: no longer read from .env — configured via Telegram (see plugins/setup.py).
# URL is the public base URL for the web portal (e.g. https://yourapp.up.railway.app).
# WEB_SERVER simply turns on whenever a URL is configured — one setting instead
# of two that have to agree with each other.
STREAM_MODE: bool = bool(settings_store.get("stream_mode", True))
URL: str          = (settings_store.get("url") or "").strip().rstrip("/")
WEB_SERVER: bool  = bool(URL)

# ── Link expiry (days). None/0 = permanent. Configured via Telegram. ─────────
LINK_EXPIRY_DAYS: int | None = settings_store.get("link_expiry_days") or None

# ── Misc ──────────────────────────────────────────────────────────────────────
TG_BOT_WORKERS: int = _int_env("TG_BOT_WORKERS", 4)
# NOTE: no longer read from .env — configured via Telegram.
MONGO_URI: str      = (settings_store.get("mongo_uri") or "").strip()

# ── Concurrency ───────────────────────────────────────────────────────────────
# BUG FIX (stuck-loading on a 2nd simultaneous download/stream):
# Pyrogram gates every file transfer (stream_media / download_media) behind a
# semaphore whose size = max_concurrent_transmissions. Its DEFAULT IS 1, so the
# second concurrent download/stream blocks until the first one finishes — the
# browser just sits on "loading". Raised to 200 so effectively every user's
# request gets its own slot immediately — no queueing behind another user's
# in-progress download/stream in normal, real-world traffic. NOTE: no longer
# read from .env — configured via Telegram (/settings).
MAX_CONCURRENT_TRANSMISSIONS: int = int(settings_store.get("max_concurrent_transmissions", 200) or 200)
if MAX_CONCURRENT_TRANSMISSIONS < 1:
    MAX_CONCURRENT_TRANSMISSIONS = 1

# Whether the first-run Telegram setup wizard has been completed.
SETUP_COMPLETE: bool = settings_store.is_setup_complete()

if not SETUP_COMPLETE:
    print(
        "\n"
        "==============================================================\n"
        " ℹ️  First run: no admin configured yet\n"
        "==============================================================\n"
        " The bot will print a rotating 6-digit claim code to this\n"
        " console every 2 minutes (see owner_claim.py) — whoever's\n"
        " watching this terminal reads the current code and sends it to\n"
        " the bot in Telegram to become admin.\n"
        "==============================================================\n",
        file=sys.stderr,
        flush=True,
    )

# Where the in-memory DB persists itself when no MONGO_URI is configured, so
# that generated links survive a process restart / redeploy (404 fix).
LOCAL_DB_PATH: str = os.environ.get("LOCAL_DB_PATH", "local_db.json").strip() or "local_db.json"

# ── Uploader info block ───────────────────────────────────────────────────────
# Whether the "👤 Uploaded by: Name / Username / User ID / Date & Time" block
# is appended to the storage-channel caption for every file. OFF by default —
# it exposes the uploader's Telegram identity (name, @username, numeric user
# ID) inside DB_CHANNEL, which isn't something every deployment wants shown
# automatically. Configurable via /settings (not part of the required setup
# wizard steps, so it never forces a decision on first run).
#
# BUG FIX: this used to be a plain module-level constant, snapshotted once
# from settings_store at import time (process start). Toggling the setting
# later via /settings wrote the new value into settings_store just fine, but
# this constant never changed, so the toggle silently had no effect until
# the bot process was restarted — which is why uploader info never showed
# up (or never turned off) after flipping it in /settings.
#
# Fix: expose it as a function that reads settings_store live on every call,
# and use module __getattr__ so existing `cfg.SHOW_UPLOADER_INFO` call sites
# (e.g. in utils.py) keep working unchanged, but now always resolve to the
# current value in settings_store instead of a stale import-time snapshot.


def show_uploader_info() -> bool:
    return bool(settings_store.get("show_uploader_info", False))


def auto_delete_seconds() -> int:
    """
    Seconds to wait after the bot replies before deleting both the user's
    uploaded message and the bot's reply. 0 = disabled. Read live (same
    __getattr__ pattern as SHOW_UPLOADER_INFO above) so toggling it via
    /settings takes effect immediately, no restart needed — there's
    nothing baked into Client/DB startup for this one.
    """
    try:
        val = int(settings_store.get("auto_delete_seconds", 0) or 0)
    except (TypeError, ValueError):
        val = 0
    return max(0, val)


def custom_caption_template() -> str:
    """
    Admin-defined caption template (see plugins/caption.py for placeholder
    syntax and the /setcaption command). Empty = use the built-in default.
    Read live so a change via Telegram applies to the very next upload with
    no restart needed — nothing about this is baked into process startup.
    """
    return (settings_store.get("custom_caption_template", "") or "").strip()


def maintenance_mode() -> bool:
    """
    True when the admin has paused new uploads for non-admin users via
    /maintenance. Read live so toggling it applies to the very next
    message with no restart needed.
    """
    return bool(settings_store.get("maintenance_mode", False))


def __getattr__(name):
    if name == "SHOW_UPLOADER_INFO":
        return show_uploader_info()
    if name == "AUTO_DELETE_SECONDS":
        return auto_delete_seconds()
    if name == "CUSTOM_CAPTION_TEMPLATE":
        return custom_caption_template()
    if name == "MAINTENANCE_MODE":
        return maintenance_mode()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
