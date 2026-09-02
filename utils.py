"""Utility helpers shared across plugins."""

import asyncio
import logging
import mimetypes
import os
import sys
from datetime import datetime
from typing import Union
from zoneinfo import ZoneInfo, available_timezones

from pyrogram import Client
from pyrogram.types import Message
from pyrogram.enums import ChatMemberStatus

from info import FORCE_SUB_CHANNEL, ADMINS

# Membership states that count as "subscribed" for force-sub checks.
_SUBSCRIBED_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
}

logger = logging.getLogger(__name__)


# ── Streamable media detection (single source of truth) ─────────────────────
# Used by BOTH plugins/file_handler.py (the bot's Telegram reply — decides
# whether to show a Stream button/link) AND web/app.py (the web page —
# decides whether to show the in-browser player). Previously each module had
# its own separate, out-of-sync definition:
#
#   - file_handler.py only checked Telegram's own message type (video/audio/
#     voice/video_note/animation). A LOT of containers Telegram doesn't
#     recognize as playable video — .mkv, .avi, .wmv, .flv, .ts, and others —
#     get uploaded by Telegram clients as a plain "document" instead, so the
#     bot's own reply silently dropped the Stream button/link even though...
#   - web/app.py ALSO checked MIME type (resolved from the filename), so the
#     web page itself could serve these fine — just not the initial Telegram
#     message that pointed at it. That mismatch is exactly why .mkv (and
#     friends) "supported streaming" inconsistently.
#
# Centralizing both the MIME/extension tables and the streamable check here
# means both surfaces always agree, and broadening format coverage only has
# to happen in one place.

STREAMABLE_MSG_TYPES = {"video", "audio", "voice", "video_note", "animation"}

# All Pyrogram message attributes that represent an uploadable file. A
# message only ever has ONE of these set, so this is really just an
# ordered list of "which attribute to check", not a priority order.
FILE_TYPES = (
    "document", "video", "audio", "photo",
    "voice", "video_note", "animation", "sticker",
)


def extract_file_info(message: Message) -> "dict | None":
    """
    Pull the (type, file_id, file_name, file_size, mime_type) tuple out of
    whichever FILE_TYPES attribute is set on `message`. Returns None if the
    message has no file attached.

    DEDUPLICATION: this used to be copy-pasted identically into both
    plugins/file_handler.py and plugins/batch.py — any future fix (like the
    filename-fallback logic below) had to be applied in two places and
    could silently drift out of sync. Centralizing it here means both
    upload paths always agree on how a file's metadata is read.
    """
    for ftype in FILE_TYPES:
        obj = getattr(message, ftype, None)
        if not obj:
            continue
        return {
            "type": ftype,
            "file_id": getattr(obj, "file_id", None),
            # Never use None/empty string as filename — fall back to a
            # clean type label so the name is never blank or duplicated
            # when it's later shown alongside the type.
            "file_name": (getattr(obj, "file_name", None) or "").strip() or ftype.replace("_", " ").title(),
            "file_size": getattr(obj, "file_size", 0) or 0,
            "mime_type": getattr(obj, "mime_type", "application/octet-stream") or "application/octet-stream",
        }
    return None


VIDEO_MIMES = {
    "video/mp4", "video/webm", "video/ogg", "video/mkv",
    "video/x-matroska", "video/avi", "video/quicktime",
    "video/x-msvideo", "video/3gpp", "video/3gpp2", "video/x-flv",
    "video/mp2t", "video/x-ms-wmv", "video/mpeg", "video/x-ms-asf",
    "video/divx", "video/x-ms-vob", "video/vnd.rn-realvideo",
    "application/vnd.rn-realmedia", "video/x-f4v",
    "video/x-dv", "video/x-nut", "video/x-yuv4mpeg", "video/x-ivf",
    "video/x-amv", "video/x-drc", "application/mxf", "video/mxf",
}
AUDIO_MIMES = {
    "audio/mpeg", "audio/ogg", "audio/wav", "audio/x-wav", "audio/flac",
    "audio/aac", "audio/mp4", "audio/x-m4a", "audio/opus",
    "audio/webm", "audio/amr", "audio/x-aiff", "audio/midi",
    "audio/x-ms-wma", "audio/3gpp",
    "audio/x-matroska", "audio/x-caf", "audio/x-pn-realaudio",
    "audio/x-ape", "audio/x-tta", "audio/x-wavpack",
    "audio/ac3", "audio/eac3", "audio/dts",
}

# Extensions mimetypes.guess_type() doesn't know, or gets wrong/generic.
EXTENSION_MIME_MAP = {
    # video containers
    ".mkv":  "video/x-matroska",
    ".ts":   "video/mp2t",
    ".m2ts": "video/mp2t",
    ".mts":  "video/mp2t",
    ".m4v":  "video/mp4",
    ".mov":  "video/quicktime",
    ".qt":   "video/quicktime",
    ".avi":  "video/x-msvideo",
    ".wmv":  "video/x-ms-wmv",
    ".asf":  "video/x-ms-asf",
    ".flv":  "video/x-flv",
    ".f4v":  "video/x-f4v",
    ".3gp":  "video/3gpp",
    ".3g2":  "video/3gpp2",
    ".mpg":  "video/mpeg",
    ".mpeg": "video/mpeg",
    ".mpe":  "video/mpeg",
    ".m1v":  "video/mpeg",
    ".m2v":  "video/mpeg",
    ".divx": "video/divx",
    ".vob":  "video/x-ms-vob",
    ".ogv":  "video/ogg",
    ".ogm":  "video/ogg",
    ".rm":   "video/vnd.rn-realvideo",
    ".rmvb": "application/vnd.rn-realmedia",
    ".webm": "video/webm",
    ".mxf":  "application/mxf",
    ".dv":   "video/x-dv",
    ".nut":  "video/x-nut",
    ".y4m":  "video/x-yuv4mpeg",
    ".ivf":  "video/x-ivf",
    ".amv":  "video/x-amv",
    ".drc":  "video/x-drc",
    # audio containers
    ".m4a":  "audio/mp4",
    ".m4b":  "audio/mp4",
    ".opus": "audio/opus",
    ".weba": "audio/webm",
    ".flac": "audio/flac",
    ".wma":  "audio/x-ms-wma",
    ".amr":  "audio/amr",
    ".aiff": "audio/x-aiff",
    ".aif":  "audio/x-aiff",
    ".mid":  "audio/midi",
    ".midi": "audio/midi",
    ".oga":  "audio/ogg",
    ".mka":  "audio/x-matroska",
    ".caf":  "audio/x-caf",
    ".ra":   "audio/x-pn-realaudio",
    ".ram":  "audio/x-pn-realaudio",
    ".ape":  "audio/x-ape",
    ".tta":  "audio/x-tta",
    ".wv":   "audio/x-wavpack",
    ".ac3":  "audio/ac3",
    ".eac3": "audio/eac3",
    ".dts":  "audio/dts",
    # subtitle sidecars (served as plain text/vtt for the player's <track>)
    ".srt":  "application/x-subrip",
    ".vtt":  "text/vtt",
    ".ass":  "text/x-ssa",
    ".ssa":  "text/x-ssa",
}


def detect_mime(file_name: str, fallback: str = "application/octet-stream") -> str:
    """
    Guess MIME type from filename, using an extended extension table for
    containers mimetypes.guess_type() doesn't know or gets wrong (.mkv,
    .ts, .avi, .wmv, .flv, and more — see EXTENSION_MIME_MAP). Falls back
    to `fallback` (typically Telegram's own reported mime_type) if nothing
    matches.
    """
    if not file_name:
        return fallback
    ext = os.path.splitext(file_name)[1].lower()
    if ext in EXTENSION_MIME_MAP:
        return EXTENSION_MIME_MAP[ext]
    guessed, _ = mimetypes.guess_type(file_name)
    return guessed or fallback


def is_streamable_media(ftype: str, mime_type: str = None, file_name: str = None) -> bool:
    """
    True if this file should get a Stream button/link and player, checking
    BOTH Telegram's own message type (video/audio/voice/video_note/
    animation) AND the MIME type resolved from the filename/extension.

    Checking only the message type (the old, pre-upgrade behavior) misses
    files Telegram itself classifies as a generic "document" — extremely
    common for containers like .mkv, .avi, .wmv, .flv, .ts, which many
    Telegram clients don't recognize as playable video and upload as a
    plain file instead, even though the file is perfectly streamable once
    it reaches our web player.
    """
    if ftype in STREAMABLE_MSG_TYPES:
        return True
    mime = mime_type or (detect_mime(file_name) if file_name else None)
    return bool(mime) and mime in (VIDEO_MIMES | AUDIO_MIMES)


def humanbytes(size: int) -> str:
    """Convert bytes to a human-readable string."""
    if not size:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


# ── Timezone support (/timezone command, see plugins/timezone.py) ──────────
# Every timestamp stored in this app (joined date, ban expiry, upload time,
# etc.) is UTC — that's the one source of truth. What THIS section adds is
# purely a per-user DISPLAY preference: when showing a time back to someone,
# convert it into their chosen timezone instead of always showing raw UTC.

# Common abbreviations people actually type, mapped to the single most
# common real-world meaning of each. (Some of these are genuinely ambiguous
# in general — "IST" is also used for Irish/Israel Standard Time, "CST" for
# China Standard Time — but Indian Standard Time / US Central are what the
# overwhelming majority of people typing these abbreviations mean, and a
# clear resolved name is always shown back for confirmation.)
_TZ_ALIASES = {
    "ist": "Asia/Kolkata", "ast": "Asia/Kolkata",
    "est": "America/New_York", "edt": "America/New_York",
    "cst": "America/Chicago", "cdt": "America/Chicago",
    "mst": "America/Denver", "mdt": "America/Denver",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "gmt": "Europe/London", "bst": "Europe/London",
    "cet": "Europe/Paris", "cest": "Europe/Paris",
    "jst": "Asia/Tokyo", "kst": "Asia/Seoul",
    "sgt": "Asia/Singapore", "hkt": "Asia/Hong_Kong",
    "aest": "Australia/Sydney", "aedt": "Australia/Sydney",
    "msk": "Europe/Moscow", "gst": "Asia/Dubai",
    "wat": "Africa/Lagos", "eat": "Africa/Nairobi",
    "utc": "UTC", "gmt+0": "UTC",
}

# A short, well-known set offered as quick-pick buttons — see
# plugins/timezone.py. Deliberately small (one representative city per
# major region) rather than exhaustive; anyone wanting something else can
# just type it.
POPULAR_TIMEZONES = [
    "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Asia/Tokyo",
    "Europe/London", "Europe/Paris", "Europe/Moscow",
    "America/New_York", "America/Chicago", "America/Los_Angeles",
    "Australia/Sydney", "UTC",
]


def resolve_timezone(query: str) -> "tuple[str | None, list[str]]":
    """
    Turn free-typed user input into a real IANA timezone name.

    Returns (resolved_name, suggestions):
      - Exact/alias match  → (resolved_name, [])
      - Ambiguous/partial  → (None, [a few close suggestions])
      - No match at all    → (None, [])

    Resolution order: exact IANA name (case-insensitive) → known
    abbreviation → substring match against real timezone names (so "kolkata"
    or "new york" finds "Asia/Kolkata" / "America/New_York" without needing
    every city hardcoded).
    """
    if not query:
        return None, []
    raw = query.strip()
    normalized = raw.lower().replace(" ", "_")

    all_zones = available_timezones()

    # BUG FIX (caught in testing): check curated aliases BEFORE a raw exact
    # IANA match. The IANA database itself contains legacy, FIXED-OFFSET
    # zones literally named "EST", "CST", "MST", "GMT", "CET" (no DST
    # awareness at all — "EST" never becomes "EDT"). Checking exact-match
    # first meant typing "EST" silently resolved to that legacy zone
    # instead of the DST-aware "America/New_York" alias below — correct in
    # winter, wrong by an hour every summer, with no error to notice it by.
    if normalized in _TZ_ALIASES:
        return _TZ_ALIASES[normalized], []

    # Exact IANA name, case-insensitive (people rarely get capitalization
    # exactly right — "asia/kolkata" should work as well as "Asia/Kolkata").
    for zone in all_zones:
        if zone.lower() == normalized:
            return zone, []

    # Substring match against the city/region part of every zone name.
    matches = sorted(
        zone for zone in all_zones
        if normalized in zone.lower().replace("_", "")
        or normalized.replace("_", "") in zone.lower().replace("_", "")
    )
    if len(matches) == 1:
        return matches[0], []
    if matches:
        return None, matches[:8]  # ambiguous — let the caller show options

    return None, []


def format_local_time(dt: "datetime | None", tz_name: "str | None" = None, include_tz: bool = True) -> str:
    """
    Format a UTC datetime for display in `tz_name` (falls back to plain
    UTC if tz_name is None/invalid/unset). Always shows the correct
    abbreviation for that EXACT date — not a hardcoded one — so e.g.
    America/New_York correctly shows EST in January and EDT in July
    instead of being wrong for half the year.
    """
    if not isinstance(dt, datetime):
        return "Unknown"

    if not tz_name or tz_name == "UTC":
        suffix = " UTC" if include_tz else ""
        return dt.strftime("%d %b %Y, %I:%M %p") + suffix

    try:
        local_dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz_name))
    except Exception:
        # Invalid/unknown zone name somehow got stored — degrade to UTC
        # rather than raising and breaking whatever was displaying this.
        suffix = " UTC" if include_tz else ""
        return dt.strftime("%d %b %Y, %I:%M %p") + suffix

    suffix = f" {local_dt.tzname()}" if include_tz else ""
    return local_dt.strftime("%d %b %Y, %I:%M %p") + suffix


def timezone_offset_str(tz_name: str) -> str:
    """'UTC+5:30'-style offset string for a given IANA name, computed for
    the CURRENT moment (correctly reflects DST if that zone observes it
    right now)."""
    try:
        now = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz_name))
    except Exception:
        return "UTC+0:00"
    offset = now.utcoffset()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    h, m = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{h}:{m:02d}"


def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def restart_bot():
    """
    Re-exec the current process so settings saved to settings_store.json
    (MONGO_URI, MAX_CONCURRENT_TRANSMISSIONS, channels, etc.) are picked up
    fresh by info.py on the next import. Some of these are baked into the
    Pyrogram Client / DB connection at startup, so a clean restart is the
    simplest way to apply them reliably.
    """
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def check_force_sub(client: Client, message: Message) -> bool:
    """
    Return True if the user is subscribed to FORCE_SUB_CHANNEL
    (or force-sub is disabled).  Sends a prompt and returns False if not.
    """
    if not FORCE_SUB_CHANNEL:
        return True

    user_id = message.from_user.id
    try:
        member = await client.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        # BUG FIX: in Pyrogram 2.x `member.status` is a ChatMemberStatus enum,
        # not a string — comparing it against ("member", "creator", ...) was
        # ALWAYS False, so every user (even subscribers) was blocked.
        if member.status in _SUBSCRIBED_STATUSES:
            return True
    except Exception:
        pass

    try:
        invite = await client.create_chat_invite_link(FORCE_SUB_CHANNEL)
        link = invite.invite_link
    except Exception:
        # BUG FIX: `lstrip('-100')` strips ANY leading '-', '1', '0' chars,
        # which mangles channel IDs (e.g. -1001234 -> "234"). Strip the exact
        # "-100" prefix once instead.
        link = f"https://t.me/c/{str(FORCE_SUB_CHANNEL).replace('-100', '', 1)}"

    await message.reply(
        "⚠️ **Join Required**\n\n"
        "You must join our channel to use this bot.\n\n"
        f"👉 [Join Channel]({link})\n\n"
        "After joining, send your command again.",
        disable_web_page_preview=True,
    )
    return False


def render_caption_template(template: str, message: Message, file_name: str, file_size: int = 0) -> str:
    """
    Fill in an admin-defined caption template with real values.

    Supported placeholders (case-insensitive, `{curly}` braces):
      {filename}   — the file's name
      {size}       — human-readable file size, e.g. "24.3 MB"
      {uploader}   — uploader's display name, or "Unknown" if unavailable
      {username}   — uploader's @username, or "(no username)"
      {user_id}    — uploader's numeric Telegram ID, or "0"
      {date}       — upload date/time, UTC

    Unknown placeholders are left as literal text rather than raising —
    a typo in an admin's template (e.g. "{flie_name}") should degrade
    gracefully to visible text the admin can spot and fix, not silently
    swallow the whole caption or crash the upload.
    """
    user = message.from_user
    uploader_name = (user.first_name or "Unknown") if user else "Unknown"
    if user and user.last_name:
        uploader_name += f" {user.last_name}"
    uploader_tag = f"@{user.username}" if (user and user.username) else "(no username)"
    user_id = user.id if user else 0
    uploaded_at = datetime.utcnow().strftime("%d %b %Y, %I:%M %p UTC")

    values = {
        "filename": file_name,
        "size": humanbytes(file_size) if file_size else "Unknown",
        "uploader": uploader_name,
        "username": uploader_tag,
        "user_id": str(user_id),
        "date": uploaded_at,
    }

    result = template
    for key, val in values.items():
        result = result.replace("{" + key + "}", val)
        result = result.replace("{" + key.upper() + "}", val)
    return result


async def build_uploader_caption(message: Message, file_name: str, file_size: int = 0, db=None) -> str:
    """
    Build the caption used when storing a file in DB_CHANNEL.

    If an admin has saved a custom caption template via /setcaption, it's
    rendered with real values (see render_caption_template above) and used
    instead of the built-in default. Falls back to the default on ANY
    rendering error, so a bad template can never break uploads.

    BUG FIX #1 — the old version prepended message.caption (which the sender
    may have written, and which could include the filename) *and* also showed
    file_name on line 1. That caused the filename to appear twice when the
    stored message was later copied back to a user.

    Default (no custom template): the storage caption contains only the file
    name, plus — if the admin has opted in via /settings (SHOW_UPLOADER_INFO,
    off by default) — a block identifying who uploaded it and when. We no
    longer re-inject message.caption into it.

    FEATURE (per-user privacy toggle, see plugins/privacy.py): a user can
    run /privacy on to hide their own identity from this caption block. When
    enabled, an anonymized placeholder is shown here INSTEAD of their real
    name/username/ID — but their real info is still stored normally in the
    users collection and remains fully visible to admins via /userinfo and
    similar admin-only tools. This only controls what's publicly written
    into the file's caption text, not what admins can look up.
    """
    import info as cfg

    template = cfg.CUSTOM_CAPTION_TEMPLATE
    if template:
        try:
            return render_caption_template(template, message, file_name, file_size)
        except Exception:
            pass  # fall through to the built-in default below

    lines = [f"📁 {file_name}"]

    if cfg.SHOW_UPLOADER_INFO:
        user = message.from_user
        uploader_name = (user.first_name or "Unknown") if user else "Unknown"
        if user and user.last_name:
            uploader_name += f" {user.last_name}"
        uploader_tag = f"@{user.username}" if (user and user.username) else "(no username)"
        user_id = user.id if user else 0

        uploaded_at = datetime.utcnow().strftime("%d %b %Y, %I:%M %p UTC")

        privacy_on = False
        if db is not None and user:
            try:
                info_doc = await db.get_user_info(user.id)
                privacy_on = bool(info_doc and info_doc.get("privacy_mode"))
            except Exception as e:
                logger.debug("Could not check privacy_mode for %s: %s", user.id, e)

        if privacy_on:
            lines += [
                "",
                "👤 **Uploaded by:** 🔒 _Anonymous (privacy mode enabled)_",
                f"▸ Date & Time: {uploaded_at}",
            ]
        else:
            lines += [
                "",
                "👤 **Uploaded by:**",
                f"▸ Name: {uploader_name}",
                f"▸ Username: {uploader_tag}",
                f"▸ User ID: `{user_id}`",
                f"▸ Date & Time: {uploaded_at}",
            ]
        # Deliberately NOT including message.caption here — it often contains
        # the filename already, which caused the duplicate display bug.

    return "\n".join(lines)


async def broadcast_message(client: Client, message: Message) -> tuple[int, int, int]:
    """
    Broadcast a message to all users.
    Returns (success_count, failed_count, total).
    """
    db = client.db  # type: ignore[attr-defined]

    success = failed = 0
    async for user in db.get_all_users():
        uid = user["_id"]
        try:
            await message.copy(uid)
            success += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # flood-control friendly

    return success, failed, success + failed
