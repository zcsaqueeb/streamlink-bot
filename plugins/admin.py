"""
Admin commands — full suite:
/stats, /status, /users, /ban, /unban, /userinfo,
/broadcast, /addadmin, /deleteall, /recentfiles, /serverstatus
"""

import asyncio
import logging
import sys
import time
import os
import platform
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from info import ADMINS, BOT_NAME
from utils import is_admin, broadcast_message, humanbytes, format_local_time
import settings_store
import transfer_stats

logger = logging.getLogger(__name__)

admin_filter = filters.create(lambda _, __, m: m.from_user and is_admin(m.from_user.id))

BOT_START_TIME = datetime.utcnow()


def _uptime() -> str:
    delta = datetime.utcnow() - BOT_START_TIME
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    d = h // 24
    h = h % 24
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


# ── /stats ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("stats") & admin_filter & filters.private)
async def stats_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]

    total_users   = await db.total_users_count()
    total_files   = await db.total_files_count()
    total_batches = await db.total_batches_count()
    banned        = await db.get_banned_count()
    today_links   = await db.get_today_links()
    today_users   = await db.get_today_users()
    stats         = await db.get_stats()
    active_30m    = await db.active_users_count(30)
    active_today  = await db.active_users_today()

    await message.reply(
        f"📊 **{BOT_NAME} — Statistics**\n"
        f"{'─' * 30}\n\n"
        f"👥 **Users**\n"
        f"  ▸ Total: `{total_users}`\n"
        f"  ▸ New Today: `{today_users}`\n"
        f"  ▸ Active (30 min): `{active_30m}`\n"
        f"  ▸ Active Today: `{active_today}`\n"
        f"  ▸ Banned: `{banned}`\n\n"
        f"📁 **Files & Links**\n"
        f"  ▸ Total Files: `{total_files}`\n"
        f"  ▸ Links Generated: `{stats.get('links_generated', 0)}`\n"
        f"  ▸ Links Today: `{today_links}`\n"
        f"  ▸ Total Batches: `{total_batches}`\n\n"
        f"🌐 **Web Activity**\n"
        f"  ▸ Streams Served: `{stats.get('streams_served', 0)}`\n"
        f"  ▸ Downloads Served: `{stats.get('downloads_served', 0)}`\n\n"
        f"⏱️ **Uptime:** `{_uptime()}`",
    )


# ── /status ───────────────────────────────────────────────────────────────────

async def _build_admin_status_text(client: Client, refreshed: bool = False) -> str:
    """Shared body for the admin-facing bot-wide dashboard — used by both
    /status (first send) and the Refresh button (edit), so the two can
    never drift out of sync with each other."""
    db = client.db  # type: ignore[attr-defined]

    active_30m   = await db.active_users_count(30)
    active_5m    = await db.active_users_count(5)
    total_users  = await db.total_users_count()
    today_links  = await db.get_today_links()
    total_files  = await db.total_files_count()
    stats        = await db.get_stats()

    try:
        import resource
        mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        mem_str = f"`{mem_mb:.1f} MB`"
    except Exception:
        mem_str = "`N/A`"

    title = f"🟢 **Live Status** _(updated {datetime.utcnow().strftime('%H:%M:%S')} UTC)_" if refreshed else "🟢 **Live Status**"

    return (
        f"{title}\n"
        f"{'─' * 30}\n\n"
        f"👁️ **Online Now (5 min):** `{active_5m}`\n"
        f"👥 **Active (30 min):** `{active_30m}`\n"
        f"👤 **Total Users:** `{total_users}`\n\n"
        f"⚡ **Active Transfers Right Now:** `{transfer_stats.active_transfers}`\n"
        f"📶 **Session Throughput:** `{humanbytes(int(transfer_stats.session_throughput_bps()))}/s`\n\n"
        f"🔗 **Links Generated Today:** `{today_links}`\n"
        f"📁 **Total Files Stored:** `{total_files}`\n"
        f"📥 **All-Time Downloads:** `{stats.get('downloads_served', 0)}`\n"
        f"▶️ **All-Time Streams:** `{stats.get('streams_served', 0)}`\n\n"
        f"⏱️ **Uptime:** `{_uptime()}`\n"
        f"💾 **Memory:** {mem_str}\n"
        f"🐍 **Python:** `{sys.version.split()[0]}`"
    )


async def _build_personal_status_text(client: Client, user_id: int, first_name: str) -> str:
    """Per-user status — account standing, own upload totals, own active
    batch. Every user gets this; it never exposes bot-wide numbers."""
    db = client.db  # type: ignore[attr-defined]

    user_info   = await db.get_user_info(user_id) or {}
    file_stats  = await db.get_user_file_stats(user_id)
    warnings    = await db.get_warnings(user_id)
    temp_ban    = await db.get_temp_ban(user_id)
    is_banned   = await db.is_banned(user_id)
    active_batch = await db.get_user_active_batch(user_id)

    my_tz = user_info.get("timezone")
    joined_str = format_local_time(user_info.get("joined"), my_tz, include_tz=False)

    if is_banned:
        standing = "⛔ **Banned**"
    elif temp_ban:
        standing = f"⏳ **Temporarily banned** until `{format_local_time(temp_ban, my_tz)}`"
    elif warnings:
        standing = f"⚠️ **Active** — `{warnings}` warning(s) on file"
    else:
        standing = "✅ **Active** — good standing"

    lines = [
        f"👤 **Your Status** — {first_name}",
        f"{'─' * 30}\n",
        f"🪪 **Account:** {standing}",
        f"📅 **Member Since:** `{joined_str}`",
        f"🆔 **User ID:** `{user_id}`",
        f"🔒 **Privacy Mode:** {'On' if user_info.get('privacy_mode') else 'Off'} _(/privacy to change)_",
        f"🕐 **Timezone:** {my_tz or 'UTC (default)'} _(/timezone to change)_\n",
        f"📁 **Files Uploaded:** `{file_stats['file_count']}`",
        f"💾 **Total Size:** `{humanbytes(file_stats['total_size'])}`",
        f"👁️ **Views on Your Files:** `{file_stats['views']}`",
        f"▶️ **Streams of Your Files:** `{file_stats['streams']}`",
        f"⬇️ **Downloads of Your Files:** `{file_stats['downloads']}`",
    ]
    if active_batch:
        lines.append(f"\n📦 **Active Batch:** `{active_batch}` _(use /mybatch for details)_")

    return "\n".join(lines)


# ── /status ───────────────────────────────────────────────────────────────────
# Every user gets their OWN status (account standing + personal upload
# stats). Admins get the bot-wide live dashboard instead — same command,
# role-based branch, so there's one intuitive command for everyone rather
# than a separate admin-only name.

@Client.on_message(filters.command("status") & filters.private)
async def status_command(client: Client, message: Message):
    user = message.from_user

    if is_admin(user.id):
        text = await _build_admin_status_text(client)
        await message.reply(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_status")]
            ])
        )
        return

    text = await _build_personal_status_text(client, user.id, user.first_name or "there")
    await message.reply(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_mystatus")]
        ])
    )


@Client.on_callback_query(filters.regex("^refresh_status$"))
async def refresh_status_cb(client, cq):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ Admins only.", show_alert=True)
        return
    await cq.answer("Refreshing…")
    text = await _build_admin_status_text(client, refreshed=True)
    await cq.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_status")]
        ])
    )


@Client.on_callback_query(filters.regex("^refresh_mystatus$"))
async def refresh_mystatus_cb(client, cq):
    await cq.answer("Refreshing…")
    text = await _build_personal_status_text(client, cq.from_user.id, cq.from_user.first_name or "there")
    await cq.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_mystatus")]
        ])
    )


# ── /users ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("users") & admin_filter & filters.private)
async def users_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    total  = await db.total_users_count()
    banned = await db.get_banned_count()
    today  = await db.get_today_users()
    active = await db.active_users_count(30)

    lines = [
        "👥 **User Report**\n",
        f"▸ Total: `{total}`",
        f"▸ New Today: `{today}`",
        f"▸ Active (30 min): `{active}`",
        f"▸ Banned: `{banned}`",
        f"▸ Active (not banned): `{total - banned}`",
    ]

    recent = await db.get_recent_users(10)
    if recent:
        lines.append("\n**Recently Joined:**")
        for u in recent:
            uid = u.get("_id", "?")
            uname = u.get("username")
            name = u.get("name") or "Unknown"
            tag = f"@{uname}" if uname else name
            status = "🔴" if u.get("banned") else "🟢"
            lines.append(f"{status} {tag} · `{uid}`")

    await message.reply("\n".join(lines))


# ── /userinfo ─────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("userinfo") & admin_filter & filters.private)
async def userinfo_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    args = message.command

    target_id: int | None = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif len(args) > 1 and args[1].lstrip("-").isdigit():
        target_id = int(args[1])

    if not target_id:
        await message.reply(
            "**Usage:** `/userinfo` followed by a numeric user ID, "
            "or reply to that user's message with `/userinfo`.\n\n"
            "**Example:** `/userinfo 123456789`"
        )
        return

    user = await db.get_user_info(target_id)
    if not user:
        await message.reply(f"❌ User `{target_id}` not found in database.")
        return

    # Timestamps below are shown in the ADMIN's own /timezone preference —
    # they're the one reading this, not the target user, so their own
    # chosen zone is what's relevant here (may differ from the target's).
    admin_info = await db.get_user_info(message.from_user.id)
    admin_tz = admin_info.get("timezone") if admin_info else None

    joined = format_local_time(user.get("joined"), admin_tz)
    last_seen = format_local_time(user.get("last_seen"), admin_tz)

    status = "🔴 Banned" if user.get("banned") else "🟢 Active"

    warnings = user.get("warnings", 0)
    temp_ban_until = user.get("temp_ban_until")
    if isinstance(temp_ban_until, str):
        try:
            temp_ban_until = datetime.fromisoformat(temp_ban_until)
        except Exception:
            temp_ban_until = None

    if temp_ban_until and temp_ban_until > datetime.utcnow():
        spam_line = f"🚫 Temp-banned until `{format_local_time(temp_ban_until, admin_tz)}`"
    else:
        spam_line = f"⚠️ Warnings: `{warnings}/3`"

    uname = user.get("username")
    if uname:
        username_line = f"@{uname} (https://t.me/{uname})"
    else:
        username_line = f"N/A — [{user.get('name', 'this user')}](tg://user?id={target_id})"

    privacy_line = "🔒 On (uploads show as anonymous)" if user.get("privacy_mode") else "Off"
    tz_line = user.get("timezone") or "UTC (default)"

    await message.reply(
        f"👤 **User Info**\n\n"
        f"▸ **ID:** `{target_id}`\n"
        f"▸ **Name:** {user.get('name', 'N/A')}\n"
        f"▸ **Username:** {username_line}\n"
        f"▸ **Status:** {status}\n"
        f"▸ **Spam:** {spam_line}\n"
        f"▸ **Privacy Mode:** {privacy_line}\n"
        f"▸ **Their Timezone:** {tz_line}\n"
        f"▸ **Joined:** {joined}\n"
        f"▸ **Last Seen:** {last_seen}\n\n"
        f"_Note: their real name/username/ID above are always visible to "
        f"admins here, even with Privacy Mode on — that toggle only hides "
        f"their identity from the public \"Uploaded by\" caption text. "
        f"Times shown above use YOUR timezone (`{admin_tz or 'UTC'}`), not theirs._",
        disable_web_page_preview=True,
    )


# ── /ban / /unban ─────────────────────────────────────────────────────────────

@Client.on_message(filters.command("ban") & admin_filter & filters.private)
async def ban_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    args = message.command
    target_id: int | None = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif len(args) > 1 and args[1].lstrip("-").isdigit():
        target_id = int(args[1])
    if not target_id:
        await message.reply(
            "**Usage:** `/ban` followed by a numeric user ID, "
            "or reply to that user's message with `/ban`.\n\n"
            "**Example:** `/ban 123456789`"
        )
        return
    if target_id in ADMINS:
        await message.reply("⛔ Cannot ban an admin.")
        return
    success = await db.ban_user(target_id)
    if success:
        user = await db.get_user_info(target_id)
        uname = user.get("username") if user else None
        tag = f"@{uname}" if uname else f"`{target_id}`"
        await message.reply(f"✅ User {tag} (`{target_id}`) has been **banned**.")
        try:
            await client.send_message(target_id, "🚫 You have been banned from this bot.")
        except Exception:
            pass
    else:
        await message.reply(f"❌ User `{target_id}` not found in database.")


@Client.on_message(filters.command("unban") & admin_filter & filters.private)
async def unban_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    args = message.command
    target_id: int | None = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif len(args) > 1 and args[1].lstrip("-").isdigit():
        target_id = int(args[1])
    if not target_id:
        await message.reply(
            "**Usage:** `/unban` followed by a numeric user ID, "
            "or reply to that user's message with `/unban`.\n\n"
            "**Example:** `/unban 123456789`"
        )
        return
    success = await db.unban_user(target_id)
    if success:
        user = await db.get_user_info(target_id)
        uname = user.get("username") if user else None
        tag = f"@{uname}" if uname else f"`{target_id}`"
        await message.reply(f"✅ User {tag} (`{target_id}`) has been **unbanned**.")
        try:
            await client.send_message(target_id, "✅ You have been unbanned!")
        except Exception:
            pass
    else:
        await message.reply(f"❌ User `{target_id}` not found.")


# ── /unwarn — clear spam warnings & lift a temp-ban early ───────────────────

@Client.on_message(filters.command("unwarn") & admin_filter & filters.private)
async def unwarn_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    args = message.command
    target_id: int | None = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif len(args) > 1 and args[1].lstrip("-").isdigit():
        target_id = int(args[1])
    if not target_id:
        await message.reply(
            "**Usage:** `/unwarn` followed by a numeric user ID, "
            "or reply to that user's message with `/unwarn`.\n\n"
            "**Example:** `/unwarn 123456789`"
        )
        return

    await db.reset_warnings(target_id)
    await db.clear_temp_ban(target_id)

    user = await db.get_user_info(target_id)
    uname = user.get("username") if user else None
    tag = f"@{uname}" if uname else f"`{target_id}`"
    await message.reply(f"✅ Cleared spam warnings & lifted any temp-ban for {tag} (`{target_id}`).")
    try:
        await client.send_message(
            target_id,
            "🎉 **You're All Clear!**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "An admin has cleared your spam warnings and lifted any "
            "temporary ban. You can use the bot normally again. 🚀"
        )
    except Exception:
        pass

@Client.on_message(filters.command("broadcast") & admin_filter & filters.private)
async def broadcast_command(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply(
            "📢 **Broadcast Usage**\n\n"
            "Reply to any message with `/broadcast` to send it to all users."
        )
        return
    confirm = await message.reply("📡 Broadcasting… please wait.")
    success, failed, total = await broadcast_message(client, message.reply_to_message)
    await confirm.edit(
        f"✅ **Broadcast Complete**\n\n"
        f"▸ Total: `{total}`\n"
        f"▸ Success: `{success}`\n"
        f"▸ Failed: `{failed}`"
    )


# ── /recentfiles ──────────────────────────────────────────────────────────────

@Client.on_message(filters.command("recentfiles") & admin_filter & filters.private)
async def recent_files_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    files = await db.get_recent_files(10)
    if not files:
        await message.reply("📭 No files found.")
        return
    lines = ["📁 **Recent 10 Files**\n"]
    for i, f in enumerate(files, 1):
        fid = f.get("_id", "?")
        fname = f.get("file_name", "Unknown")
        fsize = humanbytes(f.get("file_size", 0))
        saved = f.get("saved_at", "")
        if isinstance(saved, datetime):
            saved = saved.strftime("%m/%d %H:%M")
        link = f"{__import__('info').URL}/file/{fid}" if __import__('info').URL else fid

        uploader_id = f.get("uploader_id")
        uploader_tag = ""
        if uploader_id:
            uploader = await db.get_user_info(uploader_id)
            uname = uploader.get("username") if uploader else None
            uploader_tag = f" · 👤 @{uname}" if uname else f" · 👤 `{uploader_id}`"

        lines.append(f"{i}. [{fname}]({link}) · {fsize} · `{saved}`{uploader_tag}")
    await message.reply(
        "\n".join(lines),
        disable_web_page_preview=True,
    )


# ── /addadmin ─────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("addadmin") & admin_filter & filters.private)
async def add_admin(client: Client, message: Message):
    args = message.command
    target_id: int | None = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif len(args) > 1 and args[1].isdigit():
        target_id = int(args[1])

    if not target_id:
        await message.reply(
            "**Usage:** `/addadmin` followed by a numeric user ID, "
            "or reply to that user's message with `/addadmin`.\n\n"
            "**Example:** `/addadmin 123456789`"
        )
        return
    uid = target_id
    if uid not in ADMINS:
        ADMINS.append(uid)
        settings_store.set("admins", list(ADMINS))
        # Give the new admin their extended "/" command menu right away,
        # rather than only after the next bot restart.
        try:
            await client._register_bot_commands()
        except Exception:
            pass
        await message.reply(f"✅ `{uid}` added as admin.")
    else:
        await message.reply(f"ℹ️ `{uid}` is already an admin.")


# ── /admins ───────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("admins") & admin_filter & filters.private)
async def list_admins(client: Client, message: Message):
    if not ADMINS:
        await message.reply("No admins configured.")
        return
    lines = ["👮 **Admin List**\n"]
    for uid in ADMINS:
        tag = f"`{uid}`"
        try:
            chat = await client.get_chat(uid)
            if chat.username:
                tag = f"@{chat.username} (`{uid}`)"
            elif chat.first_name:
                tag = f"{chat.first_name} (`{uid}`)"
        except Exception:
            pass
        lines.append(f"▸ {tag}")
    await message.reply("\n".join(lines))


# ── /deleteall ────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("deleteall") & admin_filter & filters.private)
async def deleteall_command(client: Client, message: Message):
    await message.reply(
        "⚠️ **Are you sure?**\n\n"
        "This will delete ALL files from the database (not from Telegram).\n\n"
        "Reply `/confirmdeleteall` to confirm.",
    )


@Client.on_message(filters.command("confirmdeleteall") & admin_filter & filters.private)
async def confirm_deleteall(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    count = await db.total_files_count()
    if db._backend == "mongo":
        await db._db["files"].delete_many({})
    else:
        db._files.clear()
    await message.reply(f"🗑️ Deleted **{count}** file records from the database.")


# ── /serverstatus ─────────────────────────────────────────────────────────────

@Client.on_message(filters.command("serverstatus") & admin_filter & filters.private)
async def server_status_command(client: Client, message: Message):
    try:
        import resource
        mem = resource.getrusage(resource.RUSAGE_SELF)
        mem_mb = mem.ru_maxrss / 1024
        mem_str = f"{mem_mb:.1f} MB"
    except Exception:
        mem_str = "N/A"

    await message.reply(
        f"🖥️ **Server Status**\n\n"
        f"▸ **Python:** `{sys.version.split()[0]}`\n"
        f"▸ **Platform:** `{platform.system()} {platform.release()}`\n"
        f"▸ **Memory (RSS):** `{mem_str}`\n"
        f"▸ **Bot Uptime:** `{_uptime()}`\n"
        f"▸ **Started:** `{BOT_START_TIME.strftime('%Y-%m-%d %H:%M UTC')}`"
    )
