"""
File handler — receives files, stores to DB channel, returns download + stream + page links.
Upgraded: link expiry, better buttons, fast chunked storage, duplicate-filename fix.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import info as cfg
from info import DB_CHANNEL, URL
from utils import (
    check_force_sub, humanbytes, is_admin, build_uploader_caption,
    is_streamable_media, extract_file_info as _get_file_info,
)
import batch_state

logger = logging.getLogger(__name__)

# NOTE: FILE_TYPES and the file-metadata extraction logic that used to live
# here (as _get_file_info) moved to utils.py's extract_file_info — it was
# byte-for-byte identical to plugins/batch.py's own copy, so both upload
# paths now share one implementation instead of two copies that could
# silently drift out of sync. Imported above under its original name so no
# call site in this file needs to change.

# NOTE: streamability is now decided by utils.is_streamable_media(), which
# checks BOTH Telegram's message type AND the file's MIME/extension — see
# that function's docstring for why (.mkv and friends sent as a generic
# "document" used to silently lose their Stream button here).

# Default link expiry (None = permanent). Configured via Telegram (/setup,
# /settings) instead of .env — see cfg.LINK_EXPIRY_DAYS / settings_store.


def _build_links(file_uid: str) -> tuple:
    if not URL:
        return None, None, None
    base = URL.rstrip("/")
    return f"{base}/stream/{file_uid}", f"{base}/download/{file_uid}", f"{base}/file/{file_uid}"


def _expiry_text() -> str | None:
    if not cfg.LINK_EXPIRY_DAYS:
        return None
    if cfg.LINK_EXPIRY_DAYS == 1:
        return "1 day"
    return f"{cfg.LINK_EXPIRY_DAYS} days"


async def _delayed_auto_delete(client: Client, chat_id: int, message_ids: list[int], delay: int):
    """
    Waits `delay` seconds — NOT instant, the reply stays visible/copyable
    for that window — then deletes the given messages (the user's original
    upload + the bot's own link-reply) together in one call.

    Runs as a detached background task so it never blocks or delays the
    handler returning. Errors (message already deleted by the user/admin,
    bot lost admin rights, chat no longer reachable, etc.) are swallowed —
    a failed cleanup is not worth surfacing as a bot error to the user.
    """
    try:
        await asyncio.sleep(delay)
        await client.delete_messages(chat_id, message_ids)
    except Exception as e:
        logger.debug("Auto-delete skipped for %s in %s: %s", message_ids, chat_id, e)


@Client.on_message(
    filters.private & (
        filters.document | filters.video | filters.audio |
        filters.voice | filters.video_note | filters.animation |
        filters.sticker | filters.photo
    ),
    group=0
)
async def file_receive_handler(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id

    # BUG FIX / FEATURE (maintenance mode): checked before anything else
    # touches Telegram or the DB, so a paused bot never starts partially
    # processing a file it's about to reject. Admins are exempt so they
    # can keep testing/verifying while maintenance is in effect.
    if cfg.MAINTENANCE_MODE and not is_admin(user_id):
        await message.reply(
            "🛠️ **Maintenance in progress.**\n"
            "The bot isn't accepting new files right now — please try again "
            "shortly. Existing links keep working as normal."
        )
        return

    if batch_state.is_in_batch(user_id) or await db.get_user_active_batch(user_id):
        return

    if not await check_force_sub(client, message):
        return

    info = _get_file_info(message)
    if not info:
        return

    if not DB_CHANNEL:
        await message.reply(
            "⚠️ **Bot not configured.**\n"
            "The admin hasn't set a DB Channel yet — run `/setup` or `/settings`."
        )
        return

    processing = await message.reply("⏳ **Generating your link…**\n`[▓░░░░░░░░░] 10%`")

    # BUG FIX #1 — only pass the metadata caption to the storage channel,
    # NOT the original message caption, to avoid duplicating file names.
    db_caption = await build_uploader_caption(message, info["file_name"], info["file_size"], db=db)

    await processing.edit("⏳ **Uploading to storage…**\n`[▓▓▓░░░░░░░] 30%`")

    try:
        # PERFORMANCE FIX: message.copy() downloads the entire file from the
        # sender and re-uploads it to DB_CHANNEL byte-for-byte — on the SAME
        # shared Pyrogram connection an in-progress /stream or /download is
        # already using. Starting a second link generation while a first
        # download is still running forces that upload to fight the active
        # download for the same MTProto connection, which is exactly why
        # "generate link -> start download" feels slow to begin when
        # something else is already transferring.
        #
        # BUG FIX: `message.forward(DB_CHANNEL, drop_author=True)` was being
        # used to store the file, followed by a separate `fwd.edit_caption()`
        # to attach our metadata caption. This looked cheap ("just a
        # text-only edit"), but drop_author=True can't be done with a true
        # MTProto/Bot-API forward — hiding the "Forwarded from" byline is
        # only possible by sending the file as a brand-new message (a
        # copy), not a real forward. So under the hood this was already
        # sending a copy, but the `fwd` object handed back didn't reliably
        # reflect the resulting message's real chat_id/message_id — leading
        # every follow-up edit_caption() call to fail with:
        #   400 MESSAGE_ID_INVALID — The message id is invalid
        # Retrying didn't help because the ID itself was wrong, not stale.
        #
        # Fix: use message.copy(DB_CHANNEL, caption=db_caption) directly.
        # copy() already exists specifically to send-as-new-message (no
        # forward byline, same as drop_author was trying to achieve), it
        # accepts the caption up front so there's no separate edit call or
        # race at all, and the Message it returns is guaranteed valid and
        # freshly bound to DB_CHANNEL.
        try:
            fwd = await message.copy(DB_CHANNEL, caption=db_caption)
        except Exception as e:
            # Some message types (stickers, etc.) reject a caption on
            # copy(). Fall back to copying without one rather than losing
            # the file — but log it, since this is a real, visible reason
            # the uploader block didn't get attached, not something to hide.
            logger.warning(
                "copy() with caption failed (%s) — retrying without caption…", e
            )
            fwd = await message.copy(DB_CHANNEL)
    except Exception as e:
        logger.error("Forward to DB channel failed: %s", e)
        await processing.edit(
            "❌ **Storage Error**\n\n"
            "▸ Make sure the bot is **admin** in the storage channel\n"
            "▸ Check the DB Channel ID with `/settings`"
        )
        return

    await processing.edit("⏳ **Saving file record…**\n`[▓▓▓▓▓▓▓░░░] 70%`")

    file_uid = uuid.uuid4().hex[:12]

    # Calculate expiry
    expires_at = None
    if cfg.LINK_EXPIRY_DAYS:
        expires_at = datetime.utcnow() + timedelta(days=cfg.LINK_EXPIRY_DAYS)

    try:
        file_data = {
            "file_id":     info["file_id"],
            "file_name":   info["file_name"],
            "file_size":   info["file_size"],
            "mime_type":   info["mime_type"],
            "type":        info["type"],
            "msg_id":      fwd.id,
            "uploader_id": user_id,
            "saved_at":    datetime.utcnow().isoformat(),
        }
        if expires_at:
            file_data["expires_at"] = expires_at.isoformat()

        await db.save_file(file_uid, file_data)
    except Exception as e:
        logger.error("DB save error: %s", e)
        await processing.edit("❌ Database error. Please try again.")
        return

    await db.increment_stat("links_generated")
    await db.increment_stat("files_uploaded")

    stream_link, download_link, page_link = _build_links(file_uid)
    is_streamable = is_streamable_media(
        info["type"], mime_type=info["mime_type"], file_name=info["file_name"]
    )

    # ── Build message text ────────────────────────────────────────────────────
    if expires_at:
        expiry_line = f"\n⏳ **Expires:** {_expiry_text()} from now"
    else:
        expiry_line = "\n♾️ **Expiry:** Never (permanent)"

    # BUG FIX #1 — show the file name exactly once in the success reply
    text_lines = [
        "✅ **Link Generated Successfully!**\n",
        f"📁 **Name:** `{info['file_name']}`",
        f"💾 **Size:** `{humanbytes(info['file_size'])}`",
        f"📂 **Type:** `{info['mime_type']}`",
        expiry_line,
    ]

    if download_link:
        text_lines.append(f"\n⬇️ **Download:** `{download_link}`")
    if stream_link and is_streamable:
        text_lines.append(f"▶️ **Stream:** `{stream_link}`")
    if page_link:
        text_lines.append(f"🌐 **Web Page:** `{page_link}`")

    if not download_link:
        text_lines.append(
            "\n⚠️ **No web links available.**\n"
            "Ask the bot admin to set a Website URL via /settings to enable them."
        )

    # ── Build buttons ─────────────────────────────────────────────────────────
    buttons = []
    row1 = []
    row2 = []

    if download_link:
        row1.append(InlineKeyboardButton("⬇️ Download", url=download_link))
    if stream_link and is_streamable:
        row1.append(InlineKeyboardButton("▶️ Stream", url=stream_link))
    if page_link:
        row2.append(InlineKeyboardButton("🌐 Open Web Page", url=page_link))

    if row1:
        buttons.append(row1)
    if row2:
        buttons.append(row2)

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    await processing.edit(
        "\n".join(text_lines),
        reply_markup=markup,
        disable_web_page_preview=True,
    )

    # ── Auto-delete (delayed, not instant) ──────────────────────────────
    # Fires only AFTER the reply above has been sent — the countdown starts
    # from "bot has replied", not from upload time — and only if an admin
    # has configured a non-zero delay via /settings (off by default).
    delay = cfg.AUTO_DELETE_SECONDS
    if delay > 0:
        asyncio.create_task(_delayed_auto_delete(
            client, message.chat.id, [message.id, processing.id], delay,
        ))


@Client.on_message(filters.command("delete") & filters.private)
async def delete_file(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        await message.reply("⛔ Admins only.")
        return
    db = client.db  # type: ignore[attr-defined]
    args = message.command
    if len(args) < 2:
        await message.reply(
            "**Usage:** `/delete` followed by a file ID.\n\n"
            "**Example:** `/delete 4c06e7b21fb5`\n\n"
            "Find file IDs with `/recentfiles`."
        )
        return
    deleted = await db.delete_file(args[1])
    if deleted:
        await message.reply(f"✅ File `{args[1]}` removed from database.")
    else:
        await message.reply(f"❌ File `{args[1]}` not found.")
