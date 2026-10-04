"""
Dev utility — a minimal, standalone Telegram login smoke test. Not used by
the bot itself (bot.py never imports this); useful for confirming your
API_ID/API_HASH/BOT_TOKEN are valid before wiring up the full bot.

Usage (from the repo root):
    python scripts/send_test_message.py
"""

import asyncio
import sys
import os

if sys.version_info >= (3, 10):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

# This script lives in scripts/, one level below the repo root where info.py
# and .env actually are — add the parent directory to sys.path so `import
# info` resolves correctly no matter what directory this is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from pyrogram import Client
from info import API_ID, API_HASH, BOT_TOKEN, DB_CHANNEL


async def main():
    print("Connecting to Telegram…")
    # in_memory=True: same reasoning as bot.py's main Client — this script
    # authenticates purely with BOT_TOKEN, so there's no session worth
    # persisting to disk, and skipping the on-disk session file avoids
    # ever leaving a stray "script_session.session" file behind or risking
    # the AUTH_BYTES_INVALID failure mode a corrupted/shared session file
    # can cause (see bot.py for the full explanation).
    async with Client("script_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True) as app:
        me = await app.get_me()
        print(f"Logged in as @{me.username} (id={me.id})")

        # Example: send a test message to the log channel
        # await app.send_message(LOG_CHANNEL, "Script test message")

        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
