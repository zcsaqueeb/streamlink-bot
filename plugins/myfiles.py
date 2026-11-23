"""
Self-service commands for people who upload files.

/mystats   how much you've uploaded and how much it's being used
/search    find one of your own links by (part of) its file name
/expire    make a link stop working after a set time, or never
/mydata    what this bot has stored about your account

Each works only on the caller's own files (admins may manage any link's
expiry), and reads the same records /mylinks does.
"""

import logging
from datetime import timedelta

from pyrogram import Client, filters
from pyrogram.types import Message

import info as cfg
from clock import utc_now
from plugins.links import _clip, _code, _download_url, _page_url
from utils import (
    expiry_datetime, format_local_time, humanbytes, is_admin, is_expired,
    parse_duration, parse_file_uid,
)

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 10
MIN_EXPIRY = timedelta(minutes=1)
MAX_EXPIRY = timedelta(days=365)


def _fmt_remaining(delta: timedelta) -> str:
    secs = max(0, int(delta.total_seconds()))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{max(1, minutes)}m"


async def _user_tz(db, user_id: int):
    try:
        info_doc = await db.get_user_info(user_id)
    except Exception:
        info_doc = None
    return (info_doc or {}).get("timezone")


# ── /mystats ────────────────────────────────────────────────────────────────

async def build_mystats(db, user_id: int) -> str:
    """The stats text. Shared by /mystats and the "My Stats" button on /start,
    which used to show a thinner, different version of the same numbers."""
    try:
        st = await db.get_uploader_stats(user_id)
    except Exception as e:
        logger.warning("stats lookup failed for %s: %s", user_id, e)
        return "⚠️ I couldn't load your stats just now — please try again in a moment."

    if not st["files"]:
        return (
            "📊 **No stats yet** — you haven't uploaded anything.\n\n"
            "Send me a file and I'll start counting."
        )

    lines = [
        "📊 **Your stats**\n",
        f"📁 **Files:** {st['files']:,} ({humanbytes(st['bytes'])})",
        f"👁 **Page views:** {st['views']:,}",
        f"▶️ **Streams:** {st['streams']:,}",
        f"⬇️ **Downloads:** {st['downloads']:,}",
    ]
    if st["locked"]:
        lines.append(f"🔒 **Password-protected links:** {st['locked']:,}")
    top = st["top"]
    if top:
        entry = f"{_clip(top['name'], 50)} — {top['score']:,} total"
        if cfg.URL:
            entry = f"[{_clip(top['name'], 50)}]({_page_url(top['uid'])}) — {top['score']:,} total"
        lines.append(f"\n🏆 **Most popular:** {entry}")
    lines.append("\nSee them all with /mylinks, or find one with /search.")
    return "\n".join(lines)


@Client.on_message(filters.command("mystats") & filters.private)
async def mystats_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    await message.reply(
        await build_mystats(db, message.from_user.id), disable_web_page_preview=True
    )


# ── /search ─────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("search") & filters.private)
async def search_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    parts = (message.text or "").split(None, 1)
    query = parts[1].strip() if len(parts) > 1 else ""

    if len(query) < 2:
        await message.reply(
            "🔎 **Search your links**\n\n"
            "`/search <part of the file name>`\n"
            "e.g. `/search holiday` — finds every file of yours with "
            "\"holiday\" in its name."
        )
        return

    # One extra row tells us whether there are more than we're showing.
    found = await db.search_files_by_uploader(message.from_user.id, query, SEARCH_LIMIT + 1)
    shown = found[:SEARCH_LIMIT]
    if not shown:
        await message.reply(
            f"🔎 No file of yours has **{_clip(query, 40)}** in its name.\n\n"
            "Try fewer letters, or browse everything with /mylinks."
        )
        return
    if not cfg.URL:
        await message.reply(
            "⚠️ **Web links aren't configured (no `URL` set)**, so I can't "
            "show links yet. An admin needs to set the site URL first."
        )
        return

    more = len(found) > SEARCH_LIMIT
    lines = [f"🔎 **Results for** {_clip(query, 40)}\n"]
    for i, f in enumerate(shown, 1):
        uid = f.get("_id") or "?"
        mark = "🔒 " if f.get("lock") else ""
        lines.append(
            f"**{i}.** {mark}{_clip(f.get('file_name') or 'Unnamed file', 60)} "
            f"· `{humanbytes(int(f.get('file_size') or 0))}`"
        )
        lines.append(f"   [Open page]({_page_url(uid)}) · [Download]({_download_url(uid)})")
    if more:
        lines.append("\n…and more. Add a few more letters to narrow it down.")
    await message.reply("\n".join(lines), disable_web_page_preview=True)


# ── /expire ─────────────────────────────────────────────────────────────────

_EXPIRE_USAGE = (
    "⏳ **Set when a link stops working**\n\n"
    "`/expire <file ID or link> <time>`\n"
    "Time examples: `30m`, `12h`, `7d`, `2w`, `1d12h` (from now), "
    "or `never` for a permanent link.\n\n"
    "Send just `/expire <file ID or link>` to see its current expiry."
)


@Client.on_message(filters.command("expire") & filters.private)
async def expire_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id
    parts = (message.text or "").split(None, 2)

    if len(parts) < 2:
        await message.reply(_EXPIRE_USAGE)
        return

    file_uid = parse_file_uid(parts[1])
    meta = await db.get_file(file_uid)
    if not meta:
        await message.reply(f"❌ No file found with ID `{_code(file_uid)}`.")
        return
    if meta.get("uploader_id") != user_id and not is_admin(user_id):
        await message.reply("⛔ Not your file — you can only change your own links.")
        return

    tz = await _user_tz(db, user_id)
    name = _clip(meta.get("file_name") or file_uid, 60)
    current = expiry_datetime(meta)

    # No time given: report the current setting.
    if len(parts) < 3:
        if current is None:
            await message.reply(f"♾️ **{name}** never expires.")
        elif is_expired(meta):
            await message.reply(
                f"⌛ **{name}** expired on {format_local_time(current, tz)}.\n"
                "Give it a new time with `/expire <id> <time>`."
            )
        else:
            await message.reply(
                f"⏳ **{name}** expires on {format_local_time(current, tz)} "
                f"(in {_fmt_remaining(current - utc_now())})."
            )
        return

    # If the admin set a server-wide link lifetime, users may only SHORTEN a
    # link below it. Without this, `never` or a huge number would let anyone
    # quietly undo the admin's policy (admins themselves are not limited).
    policy_days = 0 if is_admin(user_id) else int(cfg.LINK_EXPIRY_DAYS or 0)
    ceiling = None
    if policy_days:
        started = meta.get("saved_at")
        if not hasattr(started, "year"):
            started = utc_now()
        ceiling = started + timedelta(days=policy_days)

    when = parts[2].strip().lower()
    if when in ("never", "off", "none", "permanent", "forever"):
        if ceiling is not None:
            await message.reply(
                f"⚠️ This server keeps links for at most **{policy_days} days**, "
                "so a link can't be made permanent here. You can still shorten "
                "it, e.g. `/expire <id> 7d`."
            )
            return
        await db.set_file_expiry(file_uid, None)
        await message.reply(f"♾️ **{name}** is now permanent — it won't expire.")
        return

    delta = parse_duration(when)
    if delta is None:
        await message.reply(
            f"❓ I couldn't read `{_code(when)}` as a time.\n\n{_EXPIRE_USAGE}"
        )
        return
    if delta < MIN_EXPIRY:
        await message.reply("⚠️ The shortest expiry is 1 minute.")
        return
    if delta > MAX_EXPIRY:
        await message.reply(
            "⚠️ The longest expiry is 365 days. Use `never` for a permanent link."
        )
        return

    expires_at = utc_now() + delta
    if ceiling is not None and ceiling <= utc_now():
        await message.reply(
            f"⚠️ This link is already older than this server's {policy_days}-day "
            "limit, so its expiry can't be extended. Ask an admin if you need it kept."
        )
        return
    if ceiling is not None and expires_at > ceiling:
        await message.reply(
            f"⚠️ This server keeps links for at most **{policy_days} days** "
            f"from upload, so this one can last until "
            f"{format_local_time(ceiling, tz)} at the latest.\n"
            "Pick a shorter time, e.g. `/expire <id> 7d`."
        )
        return
    await db.set_file_expiry(file_uid, expires_at)
    await message.reply(
        f"⏳ **{name}** will stop working on {format_local_time(expires_at, tz)} "
        f"(in {_fmt_remaining(delta)}).\n\n"
        "Change it any time with `/expire`, or use `never` to make it permanent."
    )


# ── /mydata ─────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("mydata") & filters.private)
async def mydata_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user = message.from_user
    doc = await db.get_user_info(user.id) or {}
    tz = doc.get("timezone")
    try:
        st = await db.get_uploader_stats(user.id)
    except Exception as e:
        logger.warning("mydata stats lookup failed for %s: %s", user.id, e)
        await message.reply("⚠️ I couldn't load your data just now — please try again in a moment.")
        return

    def _when(key: str) -> str:
        return format_local_time(doc.get(key), tz) if doc.get(key) else "—"

    name = doc.get("name") or user.first_name or "—"
    uname = f"@{doc['username']}" if doc.get("username") else "—"
    await message.reply(
        "🗂 **What I have stored about your account**\n\n"
        f"🆔 **User ID:** `{user.id}`\n"
        f"👤 **Name:** {_clip(name, 60)}\n"
        f"🔖 **Username:** {_clip(uname, 40)}\n"
        f"📅 **First seen:** {_when('joined')}\n"
        f"🕐 **Last active:** {_when('last_seen')}\n"
        f"🌍 **Timezone:** {tz or 'not set (UTC)'}\n"
        f"🔒 **Privacy mode:** {'on' if doc.get('privacy_mode') else 'off'}\n\n"
        f"📁 **Your links:** {st['files']} ({humanbytes(st['bytes'])})\n\n"
        "• See your links with /mylinks, and remove any with /unlink.\n"
        "• Hide your identity in upload captions with /privacy.\n"
        "• Change your timezone with /timezone."
    )
