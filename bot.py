"""
File-to-Link Telegram Bot — Python 3.10+ compatible
"""

import asyncio
import logging
import sys
import os

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# ── Python 3.12–3.14 compatibility shim ───────────────────────────────────────
# Pyrogram 2.0.106's `pyrogram/sync.py` calls `asyncio.get_event_loop()` AT IMPORT
# TIME (the moment we run `from pyrogram import ...`). On Python 3.12+ that call is
# deprecated, and on Python 3.14 it raises:
#     RuntimeError: There is no current event loop in thread 'MainThread'.
# because Python no longer auto-creates a loop for you. We fix it by making sure a
# current event loop exists in the main thread BEFORE Pyrogram is imported below.
# (asyncio.run(main()) at the bottom still creates/manages its own loop for the
# actual run — this shim only satisfies Pyrogram's import-time check.)
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

for _log in ("pyrogram.session.auth", "pyrogram.session.session",
             "pyrogram.connection.connection", "pyrogram.client",
             "pyrogram.dispatcher", "pyrogram.sync",
             # Silences: "TgCrypto is missing! Pyrogram will work the same,
             # but at a much slower speed." TgCrypto is already listed in
             # requirements.txt — installing it (pip install TgCrypto) gives
             # the real speedup; this just hides the warning either way.
             "pyrogram.crypto"):
    logging.getLogger(_log).setLevel(logging.ERROR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Terminal styling for the startup banner ──────────────────────────────────
# Plain ANSI codes — no extra dependency. Auto-disables on terminals that
# don't do color (dumb terminals, some CI/log collectors) or if NO_COLOR is set.
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text

_RED    = lambda t: _c("1;31", t)
_GOLD   = lambda t: _c("1;33", t)
_CYAN   = lambda t: _c("1;36", t)
_GREEN  = lambda t: _c("1;32", t)
_DIM    = lambda t: _c("2", t)
_BOLD   = lambda t: _c("1", t)

_TITLE_ART = r"""
   _____ _______ _____  ______          __  __ _      _____ _   _ _  __
  / ____|__   __|  __ \|  ____|   /\   |  \/  | |    |_   _| \ | | |/ /
 | (___    | |  | |__) | |__     /  \  | \  / | |      | | |  \| | ' /
  \___ \   | |  |  _  /|  __|   / /\ \ | |\/| | |      | | | . ` |  <
  ____) |  | |  | | \ \| |____ / ____ \| |  | | |____ _| |_| |\  | . \
 |_____/   |_|  |_|  \_\______/_/    \_\_|  |_|______|_____|_| \_|_|\_\
"""


from pyrogram import Client, idle
from pyrogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from info import BOT_TOKEN, API_ID, API_HASH, LOG_CHANNEL, ADMINS, BOT_NAME, WEB_SERVER, WEB_SERVER_PORT, URL, TG_BOT_WORKERS, MAX_CONCURRENT_TRANSMISSIONS, CREATOR_NAME
import settings_store
import owner_claim
from database.database import Database

_errors = []
if not BOT_TOKEN: _errors.append("BOT_TOKEN is missing")
if not API_ID:    _errors.append("API_ID is missing or 0")
if not API_HASH:  _errors.append("API_HASH is missing")
if _errors:
    sys.exit("❌ Fix your .env:\n  " + "\n  ".join(_errors))


class Bot(Client):

    def __init__(self):
        super().__init__(
            name="FileToLinkBot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            plugins=dict(root="plugins"),
            sleep_threshold=15,
            # BUG FIX: TG_BOT_WORKERS was read from env and shown in the
            # startup card, but never actually passed to the client — so the
            # worker count was silently stuck at Pyrogram's default.
            workers=TG_BOT_WORKERS,
            # BUG FIX (stuck-loading on concurrent downloads):
            # Pyrogram's default max_concurrent_transmissions is 1, so a 2nd
            # simultaneous download/stream is blocked on a semaphore until the
            # 1st finishes. Raising this lets transfers run in parallel.
            max_concurrent_transmissions=MAX_CONCURRENT_TRANSMISSIONS,
            # BUG FIX (AUTH_BYTES_INVALID on auth.ImportAuthorization):
            # Without this, Pyrogram persists an on-disk SQLite session
            # file (FileToLinkBot.session) in the container's working
            # directory. On ephemeral hosts (Railway/Render without a
            # mounted volume) or whenever a redeploy briefly overlaps the
            # previous process, two processes can end up sharing that same
            # file, or a stale/half-written auth_key from a previous boot
            # gets reused. Pyrogram opens a *separate* session per Telegram
            # media DC for file streaming by importing the authorization
            # from the main session's auth_key — if that key is corrupted
            # or was invalidated server-side, every subsequent
            # auth.ImportAuthorization call fails with AUTH_BYTES_INVALID
            # and never recovers on its own; only a fresh login fixes it.
            #
            # This bot authenticates purely with BOT_TOKEN (no phone/2FA
            # login worth preserving), so there's no benefit to persisting
            # the session to disk. in_memory=True keeps the session only in
            # RAM for this process — it never touches disk, so it can never
            # collide with another process, and every fresh start (deploy,
            # restart, crash recovery) begins from a guaranteed-clean auth
            # state instead of possibly inheriting a corrupted one.
            in_memory=True,
        )
        self.db = Database()
        self._web_runner = None
        self._claim_task = None

    async def _register_bot_commands(self):
        """
        Populate Telegram's "/" command menu. Every user gets the default
        list; each configured admin ALSO gets an extended list scoped only
        to their own chat (BotCommandScopeChat), so admin commands never
        show up as suggestions for regular users.

        Best-effort: if this fails (rare — e.g. a transient Telegram API
        hiccup at startup), we log and move on rather than blocking startup,
        since the bot is fully functional without the menu — it's a
        convenience, not a dependency.
        """
        default_commands = [
            BotCommand("start", "Show the welcome message"),
            BotCommand("help", "How to use this bot"),
            BotCommand("about", "About this bot"),
            BotCommand("batch", "Start bundling files into one link"),
            BotCommand("mybatch", "Check your current batch"),
            BotCommand("done", "Finish the current batch"),
            BotCommand("cancel", "Cancel the current batch"),
            BotCommand("settings", "View/change bot settings"),
            BotCommand("status", "Check your account status"),
            BotCommand("ping", "Check if the bot is responsive"),
            BotCommand("id", "Get your Telegram user ID"),
        ]
        admin_extra_commands = [
            c if c.command != "status" else BotCommand("status", "Your status, or bot-wide status as admin")
            for c in default_commands
        ] + [
            BotCommand("stats", "Bot usage statistics"),
            BotCommand("serverstatus", "Server resource usage"),
            BotCommand("users", "List registered users"),
            BotCommand("userinfo", "Look up a specific user"),
            BotCommand("recentfiles", "Recently uploaded files"),
            BotCommand("broadcast", "Message every user"),
            BotCommand("ban", "Ban a user"),
            BotCommand("unban", "Unban a user"),
            BotCommand("unwarn", "Clear a user's warnings"),
            BotCommand("delete", "Delete a stored file by ID"),
            BotCommand("deleteall", "Delete every stored file (careful!)"),
            BotCommand("addadmin", "Promote a user to admin"),
            BotCommand("admins", "List current admins"),
            BotCommand("setup", "Re-run the setup wizard"),
        ]

        try:
            await self.set_bot_commands(default_commands, scope=BotCommandScopeDefault())
        except Exception as e:
            logger.warning("Failed to set default bot commands: %s", e)

        for admin_id in ADMINS:
            try:
                await self.set_bot_commands(
                    admin_extra_commands, scope=BotCommandScopeChat(chat_id=admin_id)
                )
            except Exception as e:
                # Common benign cause: the admin has never opened a DM with
                # the bot yet, so Telegram can't resolve the chat scope.
                logger.debug("Could not set admin commands for %s: %s", admin_id, e)

    async def start(self):
        await self.db.connect()
        await super().start()
        me = await self.get_me()
        # Store the LIVE-fetched username (not the possibly-stale/empty
        # BOT_USERNAME env var) so the web server can build correct
        # "Open the bot" deep links on every page — see web/app.py, which
        # reads this via the `client` object it's already given.
        self.username = me.username

        # ── Telegram command menu (the "/" autocomplete list) ──────────────
        # Previously never registered at all — Telegram just showed a bare
        # text box with no command suggestions for anyone. set_bot_commands()
        # is what actually populates that menu. We set a default list every
        # user sees, then layer an extended list on top of it PER ADMIN CHAT
        # (BotCommandScopeChat) so admins get their extra commands without
        # exposing them to everyone else.
        await self._register_bot_commands()

        if WEB_SERVER:
            from web.app import start_web_server
            self._web_runner = await start_web_server(self, self.db, base_url=URL)

        from datetime import datetime
        started_at = datetime.utcnow().strftime("%d %b %Y, %I:%M %p UTC")
        db_backend = "MongoDB" if getattr(self.db, "_backend", "") == "mongo" else "In-Memory"
        web_status = "Enabled ✅" if WEB_SERVER else "Disabled ❌"

        startup_text = (
            "🟢 **B O T   I S   O N L I N E**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🤖 **Name:** {BOT_NAME}\n"
            f"🔗 **Username:** @{me.username}\n"
            f"🐍 **Python:** `{sys.version.split()[0]}`\n"
            f"💾 **Database:** `{db_backend}`\n"
            f"🌐 **Web Server:** `{web_status}`\n"
            f"📦 **Workers:** `{TG_BOT_WORKERS}`\n"
            f"⚡ **Parallel transfers:** `{MAX_CONCURRENT_TRANSMISSIONS}`\n"
            f"🕒 **Started:** `{started_at}`\n"
            f"👤 **Created by:** {CREATOR_NAME}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🚀 Ready to receive files!"
        )

        print(_RED(_TITLE_ART))
        print(_GOLD("━" * 74))
        print(f"  {_GREEN('●')} {_BOLD('ONLINE')}       @{me.username}")
        print(f"  {_CYAN('◆')} ID            {me.id}")
        print(f"  {_CYAN('◆')} Python        {sys.version.split()[0]}")
        print(f"  {_CYAN('◆')} Database      {db_backend}")
        if WEB_SERVER:
            print(f"  {_CYAN('◆')} Web Server    {URL or f'http://0.0.0.0:{WEB_SERVER_PORT}'}")
        print(f"  {_CYAN('◆')} Parallel      {MAX_CONCURRENT_TRANSMISSIONS} simultaneous transfers")
        print(f"  {_CYAN('◆')} Started       {started_at}")
        print(_GOLD("━" * 74))
        if db_backend == "In-Memory":
            print(_c("1;33",
                "  ⚠  WARNING: running WITHOUT MongoDB (in-memory store).\n"
                "     Links are persisted to disk (LOCAL_DB_PATH) so they\n"
                "     survive restarts, BUT if your host has an ephemeral\n"
                "     filesystem (e.g. Railway without a mounted volume)\n"
                "     they will be LOST on redeploy → old links return 404.\n"
                "     Set MONGO_URI for reliable, permanent links."
            ))
            print(_GOLD("━" * 74))
        print()

        if not settings_store.get("admins"):
            print(_c("1;36",
                "  👋  First run detected — no admin configured yet.\n"
                "      Open Telegram and send /start to this bot to become\n"
                "      the owner and complete setup (DB channel, MongoDB,\n"
                "      branding, etc.) right from the chat."
                "\n      🔑 Using rotating 6-digit console codes — a fresh one prints below every 2 min."
            ))
            print(_GOLD("━" * 74))
            print()

            def _print_claim_code(code: str, valid_for: int) -> None:
                print(_GOLD("━" * 74))
                print(f"  {_c('1;35', '🔑 OWNER CLAIM CODE')}   {_c('1;37;45', f' {code} ')}"
                      f"   {_DIM(f'(valid ~{valid_for // 60} min, then a new one)')}")
                print(_DIM("      Reply with this in the bot's Telegram chat to become admin."))
                print(_GOLD("━" * 74))
                print()
                # BUG FIX: without an explicit flush, this can sit in Python's
                # stdout buffer indefinitely on hosts that don't attach a TTY
                # (Railway, Render, Docker logs, etc.) — the code exists and
                # rotates correctly, it just never becomes visible. Belt-and-
                # suspenders alongside PYTHONUNBUFFERED=1 in the Dockerfile.
                sys.stdout.flush()
            self._claim_task = asyncio.create_task(owner_claim.run(_print_claim_code))

        for admin_id in ADMINS:
            try:
                await self.send_message(admin_id, startup_text)
            except Exception:
                pass

        if LOG_CHANNEL:
            try:
                await self.send_message(LOG_CHANNEL, startup_text)
            except Exception as e:
                logger.warning("LOG_CHANNEL: %s", e)

    async def stop(self):
        if self._claim_task:
            self._claim_task.cancel()
        if self._web_runner:
            await self._web_runner.cleanup()
        print("\n🛑 Bot stopped.")
        await super().stop()


async def main():
    bot = Bot()
    await bot.start()
    await idle()
    await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
