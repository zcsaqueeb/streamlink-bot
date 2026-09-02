"""
Anti-Spam Guard
================
Runs before every other handler (group=-1) and protects the bot from spam:

  - Tracks how many messages each user sends in a short time window.
  - If a user sends too many messages too quickly, they get a ⚠️ warning.
  - After 3 warnings, the user is temporarily banned for 2 hours.
  - While temp-banned, any message they send gets a "still banned, X
    remaining" reply (and is blocked from reaching other handlers).
  - Once the 2 hours pass, the very next message they send automatically
    lifts the ban and they receive a polished, bilingual welcome-back
    message — no admin action needed.

Admins and users with an active /batch session are exempt from spam
detection (batch uploads legitimately involve sending many files quickly).

BUG FIX: every non-blocking exit now raises ContinuePropagation instead of
returning. Pyrogram runs only the FIRST matching handler in a group and
then moves to the next group — a plain `return` here silently ate every
private message before any other group=-1 handler (e.g. the setup
wizard's text-input capture) ever got a look. Only the genuine "block
this message" outcomes (permanently banned / mid temp-ban / freshly
banned / just warned) still raise StopPropagation, which is intentional.
"""

import logging
import time
from datetime import datetime, timedelta

from pyrogram import Client, filters, StopPropagation, ContinuePropagation
from pyrogram.types import Message

from utils import is_admin, format_local_time
import batch_state

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────
SPAM_WINDOW_SECONDS   = 3.0   # look at messages within this many seconds
WARNING_COOLDOWN_SECS = 8.0   # don't issue more than one warning this often
MAX_WARNINGS          = 3     # warnings before a temporary ban
TEMP_BAN_HOURS        = 0.25  # length of the temporary ban (15 minutes)
TEMP_BAN_LABEL        = "15 minutes"  # human-readable form used in all messages

# ── Per-category thresholds ─────────────────────────────────────────────────
# BUG FIX / IMPROVEMENT (smarter detection): the old guard counted every
# private message the same way, so a user legitimately sending 6 files in a
# row (normal — that's exactly what this bot is for) tripped the same
# SPAM_MESSAGE_LIMIT as someone spamming 6 rapid-fire /start commands or 6
# copies of plain text. File uploads are the bot's core, expected workload
# and deserve a much higher allowance than commands or chatter, which have
# no legitimate reason to arrive in a tight burst. Each category now tracks
# its own sliding window and has its own limit, so heavy-but-normal file
# activity no longer gets flagged as spam alongside actual command/text
# flooding.
FILE_TYPES = (
    "document", "video", "audio", "photo",
    "voice", "video_note", "animation", "sticker",
)

CATEGORY_LIMITS = {
    "file":    20,   # uploading files is this bot's normal, core workload
    "command": 8,    # /start, /help, /status etc. — legitimate use is occasional
    "text":    10,   # plain chat text — wizard answers, casual messages
}


def _classify(message: Message) -> str:
    for ftype in FILE_TYPES:
        if getattr(message, ftype, None):
            return "file"
    text = message.text or message.caption or ""
    if text.startswith("/"):
        return "command"
    return "text"


# In-memory sliding-window message timestamps & cooldowns.
# _recent_msgs is now keyed by (user_id, category) so each category's burst
# is measured independently instead of one shared counter for everything.
_recent_msgs: dict[tuple[int, str], list[float]] = {}
_last_warning_at: dict[int, float] = {}


def _fmt_remaining(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "a few seconds"
    hours, rem = divmod(total_seconds, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


# ── Message templates ───────────────────────────────────────────────────────

_CATEGORY_LABEL = {
    "file": "sending files",
    "command": "sending commands",
    "text": "sending messages",
}


def _warning_text(count: int, category: str = "text") -> str:
    what = _CATEGORY_LABEL.get(category, "sending messages")
    return (
        f"⚠️ **SPAM WARNING ({count}/{MAX_WARNINGS})**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🇬🇧 **English:** You're {what} too quickly. Please slow "
        "down — repeated spam will lead to a temporary ban.\n\n"
        "🇮🇳 **हिंदी:** आप बहुत तेज़ी से मैसेज भेज रहे हैं। कृपया धीमे करें, "
        "बार-बार स्पैम करने पर आपको अस्थायी रूप से बैन कर दिया जाएगा।\n\n"
        "🇪🇸 **Español:** Estás enviando mensajes demasiado rápido. El spam "
        "repetido provocará una prohibición temporal.\n\n"
        "🇸🇦 **العربية:** أنت ترسل الرسائل بسرعة كبيرة. التكرار سيؤدي إلى "
        "حظر مؤقت.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Warnings: **{count}/{MAX_WARNINGS}**\n"
        f"🚫 After {MAX_WARNINGS} warnings → **{TEMP_BAN_LABEL} ban**"
    )


def _ban_text(until: datetime, tz_name: "str | None" = None) -> str:
    unban_str = format_local_time(until, tz_name)
    return (
        "🚫 **TEMPORARY BAN APPLIED**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🇬🇧 **English:** You've reached {MAX_WARNINGS}/{MAX_WARNINGS} spam "
        f"warnings and have been temporarily banned for **{TEMP_BAN_LABEL}**.\n\n"
        "🇮🇳 **हिंदी:** आपने स्पैम की सभी चेतावनियाँ पार कर ली हैं, इसलिए "
        f"आपको **{TEMP_BAN_LABEL}** के लिए अस्थायी रूप से बैन कर दिया "
        "गया है।\n\n"
        "🇪🇸 **Español:** Has alcanzado el límite de advertencias y has "
        f"sido suspendido temporalmente durante **{TEMP_BAN_LABEL}**.\n\n"
        "🇸🇦 **العربية:** لقد تجاوزت عدد التحذيرات المسموح به وتم حظرك "
        f"مؤقتًا لمدة **{TEMP_BAN_LABEL}**.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ **Ban Duration:** {TEMP_BAN_LABEL}\n"
        f"🕒 **Auto-Unban At:** `{unban_str}`\n\n"
        "✅ No action needed — you'll be unbanned automatically when the "
        "timer ends."
    )


def _still_banned_text(remaining: timedelta) -> str:
    return (
        "🚫 **You're Temporarily Banned**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⏳ **Time Remaining:** `{_fmt_remaining(remaining)}`\n\n"
        "🇬🇧 Please wait until the ban ends — you'll be unbanned "
        "automatically.\n"
        "🇮🇳 कृपया प्रतीक्षा करें, समय पूरा होने पर आप स्वतः अनबैन हो "
        "जाएँगे।\n\n"
        "🙏 Thanks for your patience."
    )


def _auto_unban_text() -> str:
    return (
        "🎉 **YOU'RE UNBANNED — WELCOME BACK!**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🇬🇧 **English:** Your temporary ban has ended. You can use the bot "
        "normally again. Please avoid sending messages too quickly to "
        "prevent future bans.\n\n"
        "🇮🇳 **हिंदी:** आपका अस्थायी बैन समाप्त हो गया है। अब आप बॉट का "
        "सामान्य रूप से उपयोग कर सकते हैं। कृपया भविष्य में स्पैम से बचें।\n\n"
        "🇪🇸 **Español:** Tu suspensión temporal ha terminado. Ya puedes "
        "usar el bot normalmente. Evita enviar mensajes demasiado rápido.\n\n"
        "🇸🇦 **العربية:** انتهى الحظر المؤقت. يمكنك الآن استخدام البوت "
        "بشكل طبيعي. يرجى تجنب الإرسال السريع المتكرر.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 **Thanks for your patience — enjoy the bot!** 🚀"
    )


# ── Guard handler ─────────────────────────────────────────────────────────────

@Client.on_message(filters.private, group=-1)
async def antispam_guard(client: Client, message: Message):
    user = message.from_user
    if not user:
        raise ContinuePropagation

    user_id = user.id

    # Admins are never rate-limited.
    if is_admin(user_id):
        raise ContinuePropagation

    db = client.db  # type: ignore[attr-defined]
    now = datetime.utcnow()

    # ── 1. Permanent admin ban (existing /ban system) ──────────────────────
    try:
        permanently_banned = await db.is_banned(user_id)
    except Exception as e:
        logger.warning("is_banned check failed for %s: %s", user_id, e)
        permanently_banned = False

    if permanently_banned:
        raise StopPropagation

    # ── 2. Temporary spam ban ────────────────────────────────────────────────
    try:
        temp_ban_until = await db.get_temp_ban(user_id)
    except Exception as e:
        logger.warning("get_temp_ban failed for %s: %s", user_id, e)
        temp_ban_until = None

    if temp_ban_until:
        if now < temp_ban_until:
            remaining = temp_ban_until - now
            try:
                await message.reply(_still_banned_text(remaining))
            except Exception:
                pass
            raise StopPropagation
        else:
            # Ban window has passed — auto-unban right now.
            try:
                await db.clear_temp_ban(user_id)
                await db.reset_warnings(user_id)
            except Exception as e:
                logger.warning("auto-unban cleanup failed for %s: %s", user_id, e)

            try:
                await message.reply(_auto_unban_text())
            except Exception:
                pass
            # Let this message continue to be processed normally
            # (don't stop propagation).

    # ── 3. Batch-mode users are exempt from spam detection ───────────────────
    try:
        in_batch = batch_state.is_in_batch(user_id) or bool(await db.get_user_active_batch(user_id))
    except Exception:
        in_batch = False

    if in_batch:
        raise ContinuePropagation

    # ── 4. Spam-rate detection (per-category) ────────────────────────────────
    category = _classify(message)
    limit = CATEGORY_LIMITS[category]

    now_ts = time.monotonic()
    key = (user_id, category)
    times = _recent_msgs.setdefault(key, [])
    times.append(now_ts)

    cutoff = now_ts - SPAM_WINDOW_SECONDS
    while times and times[0] < cutoff:
        times.pop(0)

    if len(times) < limit:
        raise ContinuePropagation  # normal usage for this category

    # Don't spam the user with repeated warnings for the same burst.
    last_warn = _last_warning_at.get(user_id, 0.0)
    if now_ts - last_warn < WARNING_COOLDOWN_SECS:
        raise StopPropagation

    _last_warning_at[user_id] = now_ts
    times.clear()

    try:
        warnings = await db.increment_warning(user_id)
    except Exception as e:
        logger.warning("increment_warning failed for %s: %s", user_id, e)
        warnings = 1

    if warnings >= MAX_WARNINGS:
        until = now + timedelta(hours=TEMP_BAN_HOURS)
        try:
            await db.set_temp_ban(user_id, until)
            await db.reset_warnings(user_id)
        except Exception as e:
            logger.warning("set_temp_ban failed for %s: %s", user_id, e)

        try:
            user_info = await db.get_user_info(user_id)
            user_tz = user_info.get("timezone") if user_info else None
            await message.reply(_ban_text(until, user_tz))
        except Exception:
            pass

        raise StopPropagation
    else:
        try:
            await message.reply(_warning_text(warnings, category))
        except Exception:
            pass

        raise StopPropagation
