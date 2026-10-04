"""
Maintenance mode.

Admin-only /maintenance on|off toggle. While ON, non-admin users can't
upload new files (existing links/files keep working normally — this only
pauses new intake). Every known user is notified on BOTH transitions:
when it turns on ("temporarily paused") and when it turns back off
("we're back").
"""

import asyncio
import logging

from pyrogram import Client, filters
from pyrogram.types import Message

import settings_store
from utils import is_admin

logger = logging.getLogger(__name__)

_ON_MESSAGE = (
    "🛠️ **Maintenance in progress**\n\n"
    "The bot has been temporarily paused for maintenance and isn't "
    "accepting new files right now. Anything you've already uploaded "
    "and any links you already have keep working as normal.\n\n"
    "We'll let you know the moment it's back — thanks for your patience!"
)

_OFF_MESSAGE = (
    "✅ **We're back!**\n\n"
    "Maintenance is complete and the bot is accepting files again — "
    "feel free to carry on right where you left off."
)


async def _notify_all_users(client: Client, text: str) -> tuple[int, int, int]:
    """Send `text` to every known user. Returns (success, failed, total),
    same shape as utils.broadcast_message, but sends a plain notification
    instead of copying an admin's message."""
    db = client.db  # type: ignore[attr-defined]
    success = failed = 0
    async for user in db.get_all_users():
        uid = user["_id"]
        try:
            await client.send_message(uid, text)
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # flood-control friendly, same pacing as broadcast_message
    return success, failed, success + failed


@Client.on_message(filters.command("maintenance") & filters.private)
async def maintenance_cmd(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        await message.reply("⛔ Admins only.")
        return

    args = message.text.split(None, 1)
    arg = args[1].strip().lower() if len(args) > 1 else ""

    current = bool(settings_store.get("maintenance_mode", False))

    if arg not in ("on", "off"):
        status = "🛠️ **ON** — non-admins can't upload right now" if current else "✅ **OFF** — accepting files normally"
        await message.reply(
            f"**Maintenance mode is currently:** {status}\n\n"
            "**Usage:** `/maintenance on` or `/maintenance off`\n\n"
            "Turning it on or off notifies every known user."
        )
        return

    new_state = (arg == "on")
    if new_state == current:
        await message.reply(
            f"ℹ️ Maintenance mode is already **{'ON' if current else 'OFF'}** — nothing changed."
        )
        return

    settings_store.set("maintenance_mode", new_state)

    status_msg = await message.reply(
        f"🔄 Turning maintenance mode **{'ON' if new_state else 'OFF'}** and notifying all users…"
    )

    text = _ON_MESSAGE if new_state else _OFF_MESSAGE
    success, failed, total = await _notify_all_users(client, text)

    await status_msg.edit(
        f"{'🛠️' if new_state else '✅'} **Maintenance mode is now {'ON' if new_state else 'OFF'}.**\n\n"
        f"📢 Notified {success}/{total} users"
        + (f" ({failed} unreachable — likely blocked the bot)" if failed else "")
        + "."
    )
