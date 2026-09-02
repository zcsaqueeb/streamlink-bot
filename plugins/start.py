"""
/start, /help, /about — upgraded with more buttons, smooth UX, and link expiry support.
"""

import logging
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from info import BOT_NAME, DB_CHANNEL, BOT_USERNAME, CREATOR_NAME
from utils import check_force_sub, humanbytes
from plugins.setup import maybe_claim_owner

logger = logging.getLogger(__name__)


# ── Button layouts ────────────────────────────────────────────────────────────

def _main_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 How to Use", callback_data="help"),
            InlineKeyboardButton("ℹ️ About",       callback_data="about"),
        ],
        [
            InlineKeyboardButton("🌐 Web Portal",  callback_data="web"),
            InlineKeyboardButton("⚙️ Settings",    callback_data="settings"),
        ],
        [
            InlineKeyboardButton("👨‍💻 Support",     callback_data="support"),
            InlineKeyboardButton("📊 My Stats",    callback_data="mystats"),
        ],
    ])


def _back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="home")]])


# ── /start ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    user = message.from_user
    db = client.db  # type: ignore[attr-defined]

    if await maybe_claim_owner(client, message):
        return

    try:
        await db.mark_active(user.id)
        await db.add_user(user.id, user.first_name or "", user.username or "")
    except Exception as e:
        logger.warning("Could not save user %s: %s", user.id, e)

    # ── Deep-link: /start <file_uid> ─────────────────────────────────────────
    args = message.command
    if len(args) > 1:
        file_uid = args[1]

        if not await check_force_sub(client, message):
            return

        try:
            file_meta = await db.get_file(file_uid)
        except Exception:
            file_meta = None

        if not file_meta:
            await message.reply(
                "❌ **File not found.**\n"
                "It may have been deleted. Contact the uploader for a new link.",
                reply_markup=_back_button(),
            )
            return

        # Check expiry
        from datetime import datetime
        expires_at = file_meta.get("expires_at")
        if expires_at:
            if isinstance(expires_at, str):
                try:
                    expires_at = datetime.fromisoformat(expires_at)
                except Exception:
                    expires_at = None
            if expires_at and datetime.utcnow() > expires_at:
                await message.reply(
                    "⏰ **Link Expired**\n\n"
                    "This file link has expired and is no longer valid.\n"
                    "Please ask the uploader to share a new link.",
                    reply_markup=_back_button(),
                )
                return

        try:
            msg = await client.get_messages(DB_CHANNEL, int(file_meta["msg_id"]))

            # BUG FIX #1 — build caption from stored metadata only (never
            # from msg.caption, which already contains the filename written
            # by build_uploader_caption). This guarantees the filename
            # appears exactly once in what the user receives.
            file_name = file_meta.get("file_name") or "File"
            caption = (
                f"📁 **{file_name}**\n"
                f"💾 **Size:** {humanbytes(file_meta.get('file_size', 0))}\n"
                f"📂 **Type:** `{file_meta.get('mime_type', 'unknown')}`"
            )

            # Build web links if URL is configured
            from info import URL
            buttons = []
            if URL:
                row = [InlineKeyboardButton("⬇️ Download", url=f"{URL}/download/{file_uid}")]
                if file_meta.get("type") in ("video", "audio", "voice", "video_note", "animation"):
                    row.append(InlineKeyboardButton("▶️ Stream", url=f"{URL}/stream/{file_uid}"))
                buttons.append(row)
                buttons.append([InlineKeyboardButton("🌐 Open Web Page", url=f"{URL}/file/{file_uid}")])

            markup = InlineKeyboardMarkup(buttons) if buttons else None
            # Pass caption= explicitly so Pyrogram uses our clean one-time
            # display caption instead of the raw storage-channel caption.
            await msg.copy(message.chat.id, caption=caption, reply_markup=markup)
        except Exception as e:
            logger.error("File delivery error for %s: %s", file_uid, e)
            await message.reply(
                "❌ **Could not retrieve the file.**\n"
                "The storage channel may be misconfigured. Contact @support."
            )
        return

    # ── Normal /start ─────────────────────────────────────────────────────────
    await message.reply(
        f"Hey {user.first_name} 👋\n\n"
        f"I'm **{BOT_NAME}** — send me any file and get back an instant "
        "download & stream link. No sign-up, no waiting.\n\n"
        "Multiple files? Use /batch to bundle them into one link.",
        reply_markup=_main_buttons(),
    )


# ── Callbacks ─────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex("^help$"))
async def help_callback(client, cq):
    await cq.answer()
    await cq.message.edit_text(
        "It's simple: send me a file and I'll hand you back a link — a "
        "download link, and a stream link too if it's video or audio. "
        "Share that with whoever needs it, done.\n\n"
        "A few commands worth knowing:\n"
        "/batch — bundle several files into one link instead of sending "
        "them separately\n"
        "/mybatch — see what's in your batch so far\n"
        "/ping — check how fast I'm running\n"
        "/id — grab your Telegram user ID\n\n"
        "I take documents, videos, audio, photos, voice notes, and GIFs — "
        "send whatever you've got.",
        reply_markup=_back_button(),
    )


@Client.on_callback_query(filters.regex("^about$"))
async def about_callback(client, cq):
    import sys
    await cq.answer()
    await cq.message.edit_text(
        f"I'm **{BOT_NAME}**, running on Python {sys.version.split()[0]} and "
        "Pyrogram 2.0.106, with aiohttp and Jinja2 powering the web side.\n\n"
        "Every file you send lives in a private Telegram channel — no "
        "third-party storage, nothing external. Links can stream, support "
        "seeking around in a video, and expire on a schedule if the admin's "
        "set one up.\n\n"
        f"Built by {CREATOR_NAME}.",
        reply_markup=_back_button(),
    )


@Client.on_callback_query(filters.regex("^web$"))
async def web_callback(client, cq):
    from info import URL
    await cq.answer()
    if URL:
        await cq.message.edit_text(
            "Every link I generate also has a web page version — "
            f"`{URL}` — where you can stream video right in the browser, "
            "download with a progress bar, and it all just works whether "
            "you're on your phone or a desktop.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Open Portal", url=URL)],
                [InlineKeyboardButton("🔙 Back", callback_data="home")],
            ]),
        )
    else:
        await cq.message.edit_text(
            "The web portal isn't switched on yet — the admin can turn it "
            "on by setting a Website URL in /settings.",
            reply_markup=_back_button(),
        )


@Client.on_callback_query(filters.regex("^settings$"))
async def settings_callback(client, cq):
    await cq.answer()
    await cq.message.edit_text(
        "Links don't expire by default — they'll keep working forever "
        "unless the admin's turned on an expiry window. Want to send "
        "several files as one link? /batch gets that going. Force-sub, if "
        "it's enabled, is handled by the admin too.\n\n"
        "Want any of this changed? That's a conversation with the bot admin.",
        reply_markup=_back_button(),
    )


@Client.on_callback_query(filters.regex("^mystats$"))
async def mystats_callback(client, cq):
    await cq.answer()
    db = client.db  # type: ignore[attr-defined]
    user_id = cq.from_user.id
    try:
        my = await db.get_user_file_stats(user_id)
    except Exception:
        my = {"file_count": 0, "total_size": 0, "views": 0, "streams": 0, "downloads": 0}

    await cq.message.edit_text(
        f"**📊 My Stats**\n\n"
        f"📁 **Files You've Shared:** `{my['file_count']:,}`\n"
        f"💾 **Total Size:** `{humanbytes(my['total_size'])}`\n"
        f"👁️ **Total Views:** `{my['views']:,}`\n"
        f"▶️ **Total Streams:** `{my['streams']:,}`\n"
        f"⬇️ **Total Downloads:** `{my['downloads']:,}`",
        reply_markup=_back_button(),
    )


@Client.on_callback_query(filters.regex("^support$"))
async def support_callback(client, cq):
    await cq.answer()
    await cq.message.edit_text(
        "If something's not working, a few usual suspects: Telegram caps "
        "file size at 2 GB, so anything bigger won't go through. If a link "
        "gives you a 404, the file's probably been deleted. If it says "
        "expired, just ask whoever sent it for a fresh one.\n\n"
        "Still stuck? The bot admin can help from here.",
        reply_markup=_back_button(),
    )


@Client.on_callback_query(filters.regex("^home$"))
async def home_callback(client, cq):
    await cq.answer()
    user = cq.from_user
    await cq.message.edit_text(
        f"Hey {user.first_name} 👋\n\n"
        f"I'm **{BOT_NAME}** — send me any file and get back an instant "
        "download & stream link. No sign-up, no waiting.\n\n"
        "Multiple files? Use /batch to bundle them into one link.",
        reply_markup=_main_buttons(),
    )


# ── Text commands ─────────────────────────────────────────────────────────────

@Client.on_message(filters.command("help") & filters.private)
async def help_command(client: Client, message: Message):
    await message.reply(
        "It's simple: send me a file and I'll hand you back a link — a "
        "download link, and a stream link too if it's video or audio.\n\n"
        "A few commands that might help:\n"
        "/batch — bundle several files into one link\n"
        "/mybatch — check what's in your current batch\n"
        "/done — finish the current batch\n"
        "/cancel — cancel the current batch\n"
        "/privacy — hide your identity from upload captions\n"
        "/timezone — show times in your own timezone\n"
        "/status — check your account status\n"
        "/ping — see how fast I'm running\n"
        "/id — grab your Telegram user ID\n\n"
        "If you're an admin, /status shows the bot-wide dashboard instead, "
        "and there's more on hand: /settings, /stats, /serverstatus, "
        "/users, /userinfo, /recentfiles, /broadcast, "
        "/ban, /unban, /unwarn, /delete <file_id>, /deleteall, "
        "/addadmin, /admins, /restart, /setup, /maintenance, and "
        "/setcaption, /getcaption, /resetcaption (rewrite the caption "
        "used on newly uploaded files).\n\n"
        "I can handle documents, videos, audio, photos, voice notes, GIFs, "
        "and stickers.",
        reply_markup=_main_buttons(),
    )


@Client.on_message(filters.command("about") & filters.private)
async def about_command(client: Client, message: Message):
    import sys
    await message.reply(
        f"I'm **{BOT_NAME}**, running on Python {sys.version.split()[0]} and "
        "Pyrogram 2.0.106.\n\n"
        "I turn any file you send into a permanent link, and everything's "
        "stored in a private channel — no external servers involved.\n\n"
        f"Built by {CREATOR_NAME}.",
    )
