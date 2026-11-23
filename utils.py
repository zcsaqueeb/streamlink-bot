"""Utility helpers shared across plugins."""

import asyncio
import base64
import functools
import hashlib
import hmac
import logging
import mimetypes
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from clock import utc_now
import time
from zoneinfo import ZoneInfo, available_timezones

from pyrogram import Client
from pyrogram.types import Message
from pyrogram.enums import ChatMemberStatus

import info as cfg
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


def is_video_media(ftype: str, mime_type: str = None, file_name: str = None) -> bool:
    """True for video files that can carry embedded subtitle/audio streams.

    Video notes and animations (GIFs) are excluded on purpose: neither ever
    contains a subtitle track or a second audio language, so probing them
    would download the file for nothing.
    """
    if ftype == "video":
        return True
    mime = mime_type or (detect_mime(file_name) if file_name else None)
    return bool(mime) and mime in VIDEO_MIMES


# ── Link expiry (single source of truth) ─────────────────────────────────────
# Four places have to answer "is this link dead?" — the web routes, the
# per-file block in plugins/start.py, the cleanup sweep in database.py and the
# /sub command — and each used to carry its own copy of the parse. Two hazards
# those copies had to get right:
#
#   • `expires_at` is a real datetime on records saved by the current code but
#     an ISO *string* on everything written before that fix, and the JSON
#     snapshot backend round-trips through a string regardless.
#   • A tz-aware value (an ISO string carrying an offset, or a document read
#     back from a tz_aware Mongo client) compared against utc_now()
#     raises TypeError — which surfaced as an unhandled 500 on a link that
#     should simply have been reported as expired.
#
# Both are handled once, here. The returned value is always normalized to a
# naive UTC datetime so callers can compare it with utc_now() directly.

def expiry_datetime(file_meta: dict):
    """Return the record's expiry as a naive-UTC datetime, or None."""
    value = (file_meta or {}).get("expires_at")
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    elif not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def is_expired(file_meta: dict) -> bool:
    value = expiry_datetime(file_meta)
    return value is not None and utc_now() > value


# ── Link passwords (the /lock command) ───────────────────────────────────────
#
# PBKDF2 rather than a plain hash because these guard files on a public web
# server: a leaked database dump must not hand anyone a usable password.
# 120k iterations measured ~1s per check on this host (and roughly 120ms on a
# typical VPS), which is the whole point of a KDF — but it also means
# verify_password() must never run on the event loop. Every caller goes
# through asyncio.to_thread, so a password attempt can't stall the other
# in-flight streams for a second. The iteration count is stored alongside each
# hash, so raising it later leaves existing locks verifiable.
PBKDF2_ITERATIONS = 120_000
_PBKDF2_ALGO = "sha256"
_KDF_NAME = "pbkdf2_hmac_sha256"


def hash_password(password: str) -> dict:
    """Salted PBKDF2-SHA256 digest, packaged for storage on a file record."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        _PBKDF2_ALGO, password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return {
        "kdf": _KDF_NAME,
        "iterations": PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(digest).decode(),
    }


def verify_password(password: str, record: dict) -> bool:
    """Check `password` against a hash_password() record, in constant time.

    Returns False for anything it doesn't recognise — including a record whose
    `kdf` isn't the one this build produces — rather than guessing a variant
    and quietly accepting a weaker hash.
    """
    if not record or not password:
        return False
    if record.get("kdf") != _KDF_NAME:
        return False
    try:
        salt = base64.b64decode(record["salt"])
        want = base64.b64decode(record["hash"])
        iterations = int(record.get("iterations") or PBKDF2_ITERATIONS)
    except (KeyError, TypeError, ValueError):
        return False
    got = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(got, want)


def lock_fingerprint(record: dict) -> str:
    """Short stable tag for a stored lock, used to invalidate cookies when a
    link is re-locked or unlocked. Not secret — it's derived from the salt."""
    if not record:
        return ""
    return hashlib.sha256(
        f"{record.get('kdf')}|{record.get('iterations')}|{record.get('salt')}".encode()
    ).hexdigest()[:16]


# Strong references to in-flight extraction jobs — asyncio only keeps a weak
# reference to a bare Task, so without this a task can be garbage collected
# mid-run and the extraction silently never finishes.
_TRACK_EXTRACTION_TASKS: set = set()


def schedule_track_extraction(client, db, file_uid: str, msg_id: int,
                              info: dict) -> None:
    """
    Kick off FFmpeg subtitle/audio extraction for a freshly stored video as a
    detached background task.

    Deliberately not awaited: this downloads the whole file again and runs
    FFmpeg over it, which can take minutes. The user already has their link
    by the time it's queued, and the web page picks the tracks up whenever
    the manifest lands. No-ops for non-videos, when the admin has turned the
    feature off, or when FFmpeg isn't installed on the host.
    """
    if not info or not is_video_media(info.get("type", ""),
                                      mime_type=info.get("mime_type"),
                                      file_name=info.get("file_name")):
        return
    if not cfg.EXTRACT_MEDIA_TRACKS:
        return
    from web import media
    if not media.available():
        return

    task = asyncio.create_task(media.prepare_tracks(
        client, db, file_uid, msg_id,
        file_size=info.get("file_size", 0),
        file_name=info.get("file_name", ""),
    ))
    _TRACK_EXTRACTION_TASKS.add(task)
    task.add_done_callback(_TRACK_EXTRACTION_TASKS.discard)


async def delete_file_completely(db, file_uid: str) -> tuple:
    """
    Delete a file record, anything /sub attached to it, and cached tracks.
    Returns (deleted, removed_attachment_count).

    delete_file() alone drops only the DB row, and three things were left
    behind by whichever caller forgot about them: the extracted/cached track
    directory (which went on answering /subs and /media for a link that no
    longer exists, until the hourly orphan sweep noticed), the subtitle file
    records /sub created — nothing else holds their uid, so they became
    permanent invisible rows — and those subtitles' own directories.

    Shared because there is now more than one way to delete a file (/delete
    for admins, /unlink for the uploader), and a cleanup this easy to
    half-do belongs in one place.
    """
    meta = await db.get_file(file_uid)
    attached = db.attached_subtitles(meta or {})
    if not await db.delete_file(file_uid):
        return False, 0

    from web import media   # lazy: web/__init__ imports utils
    media.cleanup_dir(file_uid)
    for sub_uid in attached:
        await db.delete_file(sub_uid)
        media.cleanup_dir(sub_uid)
    return True, len(attached)


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


@functools.lru_cache(maxsize=1)
def _get_all_zones() -> frozenset:
    """ZoneInfo.available_timezones() walks the whole tz database on disk
    every call; this set cannot change during a process lifetime, so resolve
    it once instead of on every /timezone lookup."""
    return frozenset(available_timezones())


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

    all_zones = _get_all_zones()

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
        now = utc_now().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz_name))
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


# Positive force-sub results, user_id -> monotonic time verified. Every file
# upload and every gated command used to cost a get_chat_member round trip to
# Telegram; a membership doesn't change minute to minute, so a confirmed
# subscriber is trusted for a few minutes. Only successes are cached: a user
# who isn't subscribed is re-checked every time, so joining takes effect at
# once. A user who leaves keeps access for at most this long.
_FORCE_SUB_OK_TTL = 300.0
_FORCE_SUB_OK: dict = {}


async def check_force_sub(client: Client, message: Message) -> bool:
    """
    Return True if the user is subscribed to FORCE_SUB_CHANNEL
    (or force-sub is disabled).  Sends a prompt and returns False if not.
    """
    if not FORCE_SUB_CHANNEL:
        return True

    user_id = message.from_user.id
    now = time.monotonic()
    verified = _FORCE_SUB_OK.get(user_id)
    if verified is not None and now - verified < _FORCE_SUB_OK_TTL:
        return True
    try:
        member = await client.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        # BUG FIX: in Pyrogram 2.x `member.status` is a ChatMemberStatus enum,
        # not a string — comparing it against ("member", "creator", ...) was
        # ALWAYS False, so every user (even subscribers) was blocked.
        if member.status in _SUBSCRIBED_STATUSES:
            _FORCE_SUB_OK[user_id] = now
            if len(_FORCE_SUB_OK) > 50_000:
                for uid, ts in list(_FORCE_SUB_OK.items()):
                    if now - ts >= _FORCE_SUB_OK_TTL:
                        _FORCE_SUB_OK.pop(uid, None)
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


def md_escape(text: str) -> str:
    """
    Escape MarkdownV2 metacharacters in DYNAMIC text before it goes into a
    caption that also contains intentional formatting markers.

    Pyrogram's default parse mode is ParseMode.DEFAULT (client.py:223), which
    runs every caption through its Markdown parser. Filenames are full of
    those metacharacters — `_`, `.`, `-`, `(`, `)`, `~` — so a name like
    "Movie_Name_2024_(1080p).mkv" used to be parsed as italic/entity syntax:
    the underscores were consumed as formatting delimiters and silently
    dropped from the stored caption, and a stray `_` could pair up with the
    intentional `_Anonymous ... _` markers in the uploader block and italicize
    unrelated text. Escaping the interpolated values (never the template
    itself) keeps names verbatim while leaving real formatting intact.
    """
    return _MD_SPECIAL_RE.sub(r"\\\g<0>", text or "")


_MD_SPECIAL_RE = re.compile(r"[_*\[\]()~`>#+\-=|{}.\\!]")


# Telegram rejects a caption longer than this (MEDIA_CAPTION_TOO_LONG). Bots
# don't get the 4096 limit Premium users have, so 1024 is the safe ceiling.
CAPTION_LIMIT = 1024

_IDENTITY_PLACEHOLDERS = ("{uploader}", "{username}", "{user_id}")


def template_uses_identity(template: str) -> bool:
    """True if a caption template would reveal who uploaded the file."""
    low = (template or "").lower()
    return any(ph in low for ph in _IDENTITY_PLACEHOLDERS)


def fit_caption(text: str, limit: int = CAPTION_LIMIT) -> str:
    """
    Trim a caption to Telegram's limit, at a line or word boundary.

    An over-long caption makes the copy into the storage channel fail, and the
    upload then falls back to NO caption at all - silently losing the file's
    name and info. A shortened caption is strictly better than none.
    """
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    for sep in ("\n", " "):
        i = cut.rfind(sep)
        if i >= limit * 0.6:
            cut = cut[:i]
            break
    # Never leave a dangling backslash: it would escape the ellipsis.
    return cut.rstrip().rstrip("\\") + "…"


def render_caption_template(template: str, message: Message, file_name: str,
                            file_size: int = 0, anonymous: bool = False,
                            limit: int = CAPTION_LIMIT) -> str:
    """
    Fill in an admin-defined caption template with real values.

    Supported placeholders (case-insensitive, `{curly}` braces):
      {filename}   - the file's name
      {size}       - human-readable file size, e.g. "24.3 MB"
      {uploader}   - uploader's display name, or "Unknown" if unavailable
      {username}   - uploader's @username, or "(no username)"
      {user_id}    - uploader's numeric Telegram ID, or "0"
      {date}       - upload date/time, UTC

    anonymous=True (the uploader turned on /privacy) replaces the three
    identity placeholders with an anonymous marker, so the privacy setting
    holds no matter which caption style the admin chose.

    A literal backslash-n typed in a one-line /setcaption becomes a real line
    break. Unknown placeholders are left as literal text rather than raising -
    a typo in an admin's template should degrade to visible text the admin can
    spot and fix, not swallow the whole caption or crash the upload. If the
    result is over Telegram's limit, the FILE NAME is shortened first, so the
    admin's own wording survives.
    """
    user = message.from_user
    uploader_name = (user.first_name or "Unknown") if user else "Unknown"
    if user and user.last_name:
        uploader_name += f" {user.last_name}"
    uploader_tag = f"@{user.username}" if (user and user.username) else "(no username)"
    user_id = str(user.id if user else 0)
    if anonymous:
        uploader_name = uploader_tag = "🔒 Anonymous"
        user_id = "🔒 hidden"
    uploaded_at = utc_now().strftime("%d %b %Y, %I:%M %p UTC")

    template = template.replace("\\n", "\n")

    def _fill(name: str) -> str:
        values = {
            "filename": md_escape(name),
            "size": humanbytes(file_size) if file_size else "Unknown",
            "uploader": md_escape(uploader_name),
            "username": md_escape(uploader_tag),
            "user_id": user_id,
            "date": uploaded_at,
        }
        out = template
        for key, val in values.items():
            out = out.replace("{" + key + "}", val)
            out = out.replace("{" + key.upper() + "}", val)
        return out

    name = file_name
    result = _fill(name)
    for _ in range(6):
        over = len(result) - limit
        if over <= 0 or len(name) <= 12:
            break
        keep = max(12, len(name) - over - 4)
        name = name[:keep].rstrip() + "…"
        result = _fill(name)
    return fit_caption(result, limit)


async def build_uploader_caption(message: Message, file_name: str, file_size: int = 0, db=None,
                                 template: "str | None" = None) -> str:
    """
    Build the caption used when storing a file in DB_CHANNEL.

    If an admin has saved a custom caption template via /setcaption, it's
    rendered with real values (see render_caption_template above) and used
    instead of the built-in default. Falls back to the default on ANY
    rendering error, so a bad template can never break uploads.

    Default (no custom template): the storage caption contains only the file
    name, plus - if the admin has opted in via /settings (SHOW_UPLOADER_INFO,
    off by default) - a block identifying who uploaded it and when.
    message.caption is deliberately not re-injected (it caused the filename to
    appear twice).

    PRIVACY (per-user toggle, see plugins/privacy.py): a user can run
    /privacy on to hide their identity. That is now honoured by BOTH caption
    styles. Previously only the built-in block checked it: with a custom
    template the real name, @username and ID were written straight into the
    caption, whatever the user had chosen. Real info stays stored in the users
    collection and visible to admins - this only controls the caption text.

    The result is always trimmed to Telegram's caption limit.

    `template` overrides the saved setting (an empty string forces the built-in
    caption); it exists so the /captions picker can preview any style.
    """
    import info as cfg

    user = message.from_user
    if template is None:
        template = cfg.CUSTOM_CAPTION_TEMPLATE
    show_block = cfg.SHOW_UPLOADER_INFO and not template

    # Only look the user's setting up when the caption could actually reveal
    # them - an upload with a plain template or no uploader block needs no
    # extra database call.
    privacy_on = False
    if db is not None and user and (show_block or (template and template_uses_identity(template))):
        try:
            info_doc = await db.get_user_info(user.id)
            privacy_on = bool(info_doc and info_doc.get("privacy_mode"))
        except Exception as e:
            logger.debug("Could not check privacy_mode for %s: %s", user.id, e)

    if template:
        try:
            return render_caption_template(
                template, message, file_name, file_size, anonymous=privacy_on
            )
        except Exception:
            pass  # fall through to the built-in default below

    lines = [f"📁 {md_escape(file_name)}"]

    if cfg.SHOW_UPLOADER_INFO:
        uploader_name = (user.first_name or "Unknown") if user else "Unknown"
        if user and user.last_name:
            uploader_name += f" {user.last_name}"
        uploader_tag = f"@{user.username}" if (user and user.username) else "(no username)"
        user_id = user.id if user else 0

        uploaded_at = utc_now().strftime("%d %b %Y, %I:%M %p UTC")

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
                f"▸ Name: {md_escape(uploader_name)}",
                f"▸ Username: {md_escape(uploader_tag)}",
                f"▸ User ID: `{user_id}`",
                f"▸ Date & Time: {uploaded_at}",
            ]

    return fit_caption("\n".join(lines))


_DURATION_PART = re.compile(r"(\d+)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)", re.I)
_DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str):
    """
    Parse a human duration like "30m", "12h", "7d", "2w" or "1d12h" into a
    timedelta. Returns None if it isn't a valid duration. (Callers handle
    "never"/"off" themselves, since that means "no limit", not an error.)
    """
    t = (text or "").strip().lower().replace(",", " ")
    if not t:
        return None
    pos, seconds = 0, 0
    for m in _DURATION_PART.finditer(t):
        if t[pos:m.start()].strip():
            return None                       # junk between/before the parts
        seconds += int(m.group(1)) * _DURATION_UNITS[m.group(2)[0]]
        pos = m.end()
    if pos == 0 or t[pos:].strip():
        return None                           # nothing matched, or trailing junk
    return timedelta(seconds=seconds)


_URL_ROUTES = {"file", "download", "stream", "info", "thumbnail", "subs", "media", "batch"}


def parse_file_uid(token: str) -> str:
    """
    Accept a bare file ID OR a pasted link and return the ID.

    People copy the whole link from /mylinks or the browser, not the 12
    characters at its end, and every command that wanted an ID answered
    "No file found". This takes `abc123`, `https://site/file/abc123`,
    `site/download/abc123?x=1`, with stray quotes, backticks or brackets.
    """
    t = (token or "").strip().strip("`<>()[]\"'")
    if not t or "/" not in t:
        return t
    t = t.split("#", 1)[0].split("?", 1)[0]
    parts = [x for x in t.split("/") if x]
    for i, part in enumerate(parts[:-1]):
        if part.lower() in _URL_ROUTES:
            return parts[i + 1]
    return parts[-1] if parts else ""


async def _retry_on_flood(op, chat_id: int, attempts: int = 3) -> bool:
    """
    Run `op()` (a no-arg coroutine factory), honoring Telegram's FloodWait.

    Returns True on success, False if the recipient should be counted as a
    failure (privacy block, deleted account, blocked bot, repeated floods).

    BUG FIX: broadcast loops used a bare `except Exception: failed += 1`, so a
    FloodWait — which is a "wait, then try again", not a failure — was counted
    as a permanently unreachable user. Pyrogram only auto-sleeps below its
    own sleep_threshold, so every larger backoff silently dropped that
    recipient from the broadcast.
    """
    from pyrogram.errors import FloodWait

    for attempt in range(attempts):
        try:
            await op()
            return True
        except FloodWait as e:
            wait = int(getattr(e, "value", 0) or 0)
            if wait <= 0 or attempt == attempts - 1 or wait > 300:
                logger.warning(
                    "FloodWait %ss for %s — giving up on this recipient", wait, chat_id
                )
                return False
            logger.info("FloodWait %ss while messaging %s — waiting", wait, chat_id)
            await asyncio.sleep(wait + 1)
        except Exception as e:
            logger.debug("Could not message %s: %s", chat_id, e)
            return False
    return False


async def copy_with_flood_retry(client: Client, message: Message, chat_id: int,
                                attempts: int = 3) -> bool:
    """Copy `message` to `chat_id`, honoring FloodWait. See _retry_on_flood."""
    return await _retry_on_flood(lambda: message.copy(chat_id), chat_id, attempts)


async def send_with_flood_retry(client: Client, chat_id: int, text: str,
                                attempts: int = 3) -> bool:
    """Send `text` to `chat_id`, honoring FloodWait. See _retry_on_flood."""
    return await _retry_on_flood(lambda: client.send_message(chat_id, text),
                                 chat_id, attempts)


# ── Background tasks ─────────────────────────────────────────────────────────
# asyncio keeps only a weak reference to a task, so a detached task with
# nothing else holding it can be garbage-collected before it finishes. This
# set is the strong reference.
_BG_TASKS: set = set()


def spawn(coro) -> "asyncio.Future":
    """Run `coro` detached from the caller, keeping it alive until it ends."""
    task = asyncio.ensure_future(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


# ── Bulk sending (broadcast / maintenance notices) ───────────────────────────
# Only one bulk send runs at a time: two overlapping ones would double the
# message rate and trip Telegram's flood limits for the whole bot.
BULK_SEND_LOCK = asyncio.Lock()

# Telegram allows a bot roughly 30 messages/second overall. Stay under it.
_BULK_PER_SECOND = 25.0
_BULK_CONCURRENCY = 16


class _Pacer:
    """Spaces call starts at least 1/rate seconds apart, across all workers."""

    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            await asyncio.sleep(delay)


async def fan_out(recipients, send_one, *, on_progress=None,
                  concurrency: int = _BULK_CONCURRENCY,
                  per_second: float = _BULK_PER_SECOND,
                  progress_every: float = 3.0) -> tuple[int, int, int]:
    """
    Deliver to every recipient with bounded concurrency and a global rate cap.

    `recipients` is an async iterable of user documents (each with "_id");
    `send_one(user_id)` must return True/False and never raise. Sending used
    to be strictly one-at-a-time: each message waited for the previous
    round trip plus a sleep, so a large user base took many minutes and held
    a Pyrogram handler worker for the whole time. Here a small worker pool
    keeps several sends in flight while the pacer holds the overall rate
    under Telegram's limit. Returns (success, failed, total).
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=concurrency * 2)
    counts = {"ok": 0, "bad": 0}
    pacer = _Pacer(per_second)

    async def _worker():
        while True:
            uid = await queue.get()
            if uid is None:
                return
            await pacer.wait()
            try:
                ok = await send_one(uid)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("send to %s failed: %s", uid, e)
                ok = False
            counts["ok" if ok else "bad"] += 1

    async def _ticker():
        while True:
            await asyncio.sleep(progress_every)
            try:
                await on_progress(counts["ok"], counts["bad"])
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # progress is cosmetic; never let it break the send

    workers = [asyncio.ensure_future(_worker()) for _ in range(concurrency)]
    ticker = asyncio.ensure_future(_ticker()) if on_progress else None
    try:
        async for user in recipients:
            await queue.put(user["_id"])
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers)
    finally:
        for t in workers + ([ticker] if ticker else []):
            if not t.done():
                t.cancel()
        await asyncio.gather(*workers, *( [ticker] if ticker else [] ),
                             return_exceptions=True)

    return counts["ok"], counts["bad"], counts["ok"] + counts["bad"]


async def broadcast_message(client: Client, message: Message,
                            on_progress=None) -> tuple[int, int, int]:
    """
    Broadcast a message to all users.
    Returns (success_count, failed_count, total).
    """
    db = client.db  # type: ignore[attr-defined]

    async def _send(uid):
        return await copy_with_flood_retry(client, message, uid)

    return await fan_out(db.get_all_users(), _send, on_progress=on_progress)


async def notify_users(client: Client, text: str,
                       on_progress=None) -> tuple[int, int, int]:
    """Send a plain text notice to every known user. Same return shape."""
    db = client.db  # type: ignore[attr-defined]

    async def _send(uid):
        return await send_with_flood_retry(client, uid, text)

    return await fan_out(db.get_all_users(), _send, on_progress=on_progress)



# --------------------------------------------------------------------------
# SECURITY: executable / script upload blocklist (malware distribution)
# --------------------------------------------------------------------------
import os as _os

_DEFAULT_BLOCKED = (
    "exe,scr,bat,cmd,com,pif,msi,msp,vbs,vbe,js,jse,wsf,wsh,ps1,psm1,hta,"
    "cpl,jar,lnk,dll,sys,reg,apk,apks,xapk,dmg,pkg,sh,bin,run,appimage,"
    "iso,inf,gadget,application,msc,scf,url,chm,docm,xlsm,pptm"
)


def blocked_extensions() -> "set[str]":
    raw = _os.environ.get("BLOCKED_EXTENSIONS", _DEFAULT_BLOCKED).strip().lower()
    if raw in ("", "none", "off", "0"):
        return set()
    return {e.strip().lstrip(".") for e in raw.split(",") if e.strip()}


def is_blocked_filename(name: str) -> "str | None":
    """Return the offending extension if `name` is blocked, else None.
    Checks every dot-part so `invoice.pdf.exe` and `a.exe.txt` are caught."""
    parts = (name or "").lower().strip().split(".")[1:]
    bad = blocked_extensions()
    for ext in parts:
        if ext.strip() in bad:
            return ext.strip()
    return None
