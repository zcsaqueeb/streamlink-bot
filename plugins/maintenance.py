"""
Maintenance mode.

Admin-only /maintenance on|off toggle. While ON, non-admin users can't
upload new files (existing links/files keep working normally — this only
pauses new intake). Every known user is notified on BOTH transitions:
when it turns on ("temporarily paused") and when it turns back off
("we're back").
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import Message

import settings_store
from utils import is_admin, notify_users, spawn, BULK_SEND_LOCK

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


async def _notify_and_report(client: Client, status_msg: Message,
                             new_state: bool) -> None:
    """Notify every user of the new state, then report on `status_msg`.

    Runs detached so the /maintenance handler returns immediately. It waits
    its turn behind any broadcast already in progress rather than overlapping
    it (which would double the send rate)."""
    label = "ON" if new_state else "OFF"
    icon = "🛠️" if new_state else "✅"
    text = _ON_MESSAGE if new_state else _OFF_MESSAGE

    async def _progress(ok: int, bad: int):
        try:
            await status_msg.edit(
                f"🔄 Maintenance mode is **{label}**. Notifying users… "
                f"`{ok + bad}` done"
            )
        except Exception:
            pass  # cosmetic progress edit (FloodWait/not-modified)

    try:
        async with BULK_SEND_LOCK:
            success, failed, total = await notify_users(
                client, text, on_progress=_progress
            )
        result = (
            f"{icon} **Maintenance mode is now {label}.**\n\n"
            f"📢 Notified {success}/{total} users"
            + (f" ({failed} unreachable — likely blocked the bot)" if failed else "")
            + "."
        )
    except Exception as e:
        logger.exception("Maintenance notification crashed")
        result = (
            f"{icon} **Maintenance mode is now {label}**, but notifying users "
            f"stopped early: `{e}`"
        )
    try:
        await status_msg.edit(result)
    except Exception:
        await status_msg.reply(result)


@Client.on_message(filters.command("maintenance") & filters.private)
async def maintenance_cmd(client: Client, message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
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
        f"🔄 Maintenance mode is now **{'ON' if new_state else 'OFF'}**. "
        "Notifying all users in the background…"
    )
    # The switch above already took effect; the notices go out detached so
    # this handler doesn't occupy an update worker while they send.
    spawn(_notify_and_report(client, status_msg, new_state))
