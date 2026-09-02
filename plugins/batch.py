"""
/batch — Collect multiple files and generate a single shareable batch link.

Usage:
  /batch     → start collecting files
  send files → each gets saved and added to the batch
  /done      → finalize and receive a single batch link
  /cancel    → cancel current batch
"""

import logging
import uuid
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import info as cfg
from info import DB_CHANNEL, URL
from utils import (
    check_force_sub, humanbytes, build_uploader_caption, is_admin,
    extract_file_info as _get_file_info,
)
import batch_state

logger = logging.getLogger(__name__)

# NOTE: FILE_TYPES and the file-metadata extraction logic that used to live
# here (as _get_file_info) moved to utils.py's extract_file_info — it was
# byte-for-byte identical to plugins/file_handler.py's own copy, so both
# upload paths now share one implementation instead of two copies that
# could silently drift out of sync. Imported above under its original name
# so no call site in this file needs to change.


def _batch_progress_markup(batch_id: str, file_count: int) -> InlineKeyboardMarkup:
    """
    Builds the Done/Cancel(/Remove Last) keyboard shown while a batch is in
    progress. Shared by batch_start, mybatch_cmd, and the "added to batch"
    confirmation so all three stay visually and functionally consistent —
    previously each built this markup separately. Remove Last only appears
    once there's at least one file to remove.
    """
    rows = [[
        InlineKeyboardButton("✅ Done", callback_data=f"batch_done_{batch_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"batch_cancel_{batch_id}"),
    ]]
    if file_count > 0:
        rows.append([InlineKeyboardButton("↩️ Remove Last File", callback_data=f"batch_undo_{batch_id}")])
    return InlineKeyboardMarkup(rows)


@Client.on_message(filters.command("batch") & filters.private)
async def batch_start(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id

    if cfg.MAINTENANCE_MODE and not is_admin(user_id):
        await message.reply(
            "🛠️ **Maintenance in progress.**\n"
            "The bot isn't accepting new files right now — please try again shortly."
        )
        return

    if not await check_force_sub(client, message):
        return

    existing = batch_state.get_batch_id(user_id) or await db.get_user_active_batch(user_id)
    if existing:
        await message.reply(
            "⚠️ **You already have an active batch!**\n\n"
            "▸ Keep sending files to add them\n"
            "▸ /done — finalize and get the batch link\n"
            "▸ /cancel — cancel and discard"
        )
        return

    batch_id = uuid.uuid4().hex[:10]
    await db.create_batch(batch_id, user_id)
    batch_state.set_batch(user_id, batch_id)

    await message.reply(
        "📦 **Batch Mode Started!**\n\n"
        f"▸ Batch ID: `{batch_id}`\n\n"
        "Now send your files one by one.\n"
        "When done, type /done to generate your batch link.\n"
        "To cancel, type /cancel.",
        reply_markup=_batch_progress_markup(batch_id, file_count=0)
    )


@Client.on_message(filters.command("done") & filters.private)
async def batch_done_cmd(client: Client, message: Message):
    await _finalize_batch(client, message, message.from_user.id)


@Client.on_message(filters.command("cancel") & filters.private)
async def batch_cancel_cmd(client: Client, message: Message):
    user_id = message.from_user.id

    # BUG FIX (/cancel dead-end during /settings): an in-progress settings
    # wizard step used to have NO way to be cancelled — /cancel only ever
    # checked for an active batch, so an admin mid-way through editing a
    # setting (or stuck on a step they didn't mean to open) who typed
    # /cancel just got told "No active batch to cancel" while the wizard
    # kept silently waiting for input on every message they sent after.
    # Check for (and clear) a pending settings wizard first so /cancel
    # works no matter which flow is actually active.
    import plugins.setup as _setup
    if user_id in _setup._WIZARD_STATE:
        _setup._WIZARD_STATE.pop(user_id, None)
        await message.reply("❌ **Setup/settings edit cancelled.** Nothing was changed.")
        return

    db = client.db  # type: ignore[attr-defined]
    batch_id = batch_state.get_batch_id(user_id) or await db.get_user_active_batch(user_id)
    if not batch_id:
        await message.reply("ℹ️ No active batch or settings edit to cancel.")
        return
    batch_state.clear_batch(user_id)
    await db.close_batch(batch_id)
    await message.reply("❌ **Batch cancelled.**")


@Client.on_message(filters.command("mybatch") & filters.private)
async def mybatch_cmd(client: Client, message: Message):
    """Show the current in-progress batch: how many files so far, with quick
    /done and /cancel actions — referenced from /help but never implemented
    until now."""
    user_id = message.from_user.id
    db = client.db  # type: ignore[attr-defined]
    batch_id = batch_state.get_batch_id(user_id) or await db.get_user_active_batch(user_id)

    if not batch_id:
        await message.reply(
            "ℹ️ **No active batch.**\n\nStart one with /batch, then send files one by one."
        )
        return

    batch = await db.get_batch(batch_id)
    files = (batch or {}).get("files", [])

    await message.reply(
        f"📦 **Active Batch**\n\n"
        f"▸ Batch ID: `{batch_id}`\n"
        f"▸ Files so far: **{len(files)}**\n\n"
        "Keep sending files to add more.",
        reply_markup=_batch_progress_markup(batch_id, file_count=len(files))
    )


@Client.on_callback_query(filters.regex(r"^batch_done_(.+)$"))
async def batch_done_cb(client, cq):
    batch_id = cq.data.split("_", 2)[2]
    await cq.answer()
    await _finalize_batch(client, cq.message, cq.from_user.id, batch_id=batch_id)


@Client.on_callback_query(filters.regex(r"^batch_cancel_(.+)$"))
async def batch_cancel_cb(client, cq):
    batch_id = cq.data.split("_", 2)[2]
    db = client.db  # type: ignore[attr-defined]
    batch_state.clear_batch(cq.from_user.id)
    await db.close_batch(batch_id)
    await cq.answer("Batch cancelled.")
    await cq.message.edit_text("❌ **Batch cancelled.**")


@Client.on_callback_query(filters.regex(r"^batch_undo_(.+)$"))
async def batch_undo_cb(client, cq):
    """
    "↩️ Remove Last File" — undoes the most recent add without cancelling
    the whole batch. Previously the only way to fix an accidental upload
    was /cancel and starting completely over, losing every file already
    added.
    """
    batch_id = cq.data.split("_", 2)[2]
    db = client.db  # type: ignore[attr-defined]

    removed_uid = await db.remove_last_file_from_batch(batch_id)
    if not removed_uid:
        await cq.answer("Nothing to remove — the batch is already empty.", show_alert=True)
        return

    batch = await db.get_batch(batch_id)
    file_count = len(batch.get("files", [])) if batch else 0

    await cq.answer("Removed the last file.")
    if file_count > 0:
        plural = "s" if file_count != 1 else ""
        text = (
            f"↩️ **Removed last file.** ({file_count} file{plural} remaining)\n\n"
            "Send more files, or type /done to finish."
        )
    else:
        text = (
            "↩️ **Removed last file.** The batch is now empty.\n\n"
            "Send a file to add it back, or /cancel to stop."
        )
    await cq.message.edit_text(
        text,
        reply_markup=_batch_progress_markup(batch_id, file_count=file_count),
    )


async def _finalize_batch(client, message, user_id: int, batch_id: str = None):
    db = client.db  # type: ignore[attr-defined]
    bid = batch_id or batch_state.get_batch_id(user_id) or await db.get_user_active_batch(user_id)

    if not bid:
        await message.reply("ℹ️ No active batch found. Start one with /batch.")
        return

    batch = await db.get_batch(bid)
    if not batch or not batch.get("files"):
        await message.reply("⚠️ **Empty batch.** Send at least one file before finishing.")
        return

    files = batch["files"]
    await db.close_batch(bid)
    batch_state.clear_batch(user_id)

    batch_url = f"{URL}/batch/{bid}" if URL else None

    # IMPROVEMENT: when a web batch page is configured, it already shows
    # every file with thumbnails, search, and a download-all button — so
    # repeating the same info as a giant text list here was both redundant
    # AND risky: Telegram caps a single message at 4096 characters, and a
    # batch of 30+ files with long names could silently exceed that and
    # fail to send at all. Just point at the page instead.
    if batch_url:
        await message.reply(
            f"✅ **Batch Ready! ({len(files)} files)**\n\n"
            f"🔗 **Batch Link:** `{batch_url}`",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📦 Open Batch Page", url=batch_url)]]
            ),
            disable_web_page_preview=True,
        )
        return

    # No web URL configured — individual links are the ONLY way to reach
    # these files, so they must always be delivered in full. Rather than
    # risk one oversized message silently failing to send past Telegram's
    # 4096-char limit, build the list in chunks and send as multiple
    # messages when needed — every file's link always gets through.
    lines = []
    for i, fuid in enumerate(files, 1):
        fm = await db.get_file(fuid)
        fname = fm.get("file_name", fuid) if fm else fuid
        lines.append(f"{i}. `{fuid}` — {fname}")

    header = f"✅ **Batch Ready! ({len(files)} files)**\n\n**Individual Links:**"
    await message.reply(header)

    SAFE_CHUNK_LEN = 3500  # comfortable margin below Telegram's 4096 cap
    chunk, chunk_len = [], 0
    for line in lines:
        if chunk_len + len(line) + 1 > SAFE_CHUNK_LEN and chunk:
            await message.reply("\n".join(chunk), disable_web_page_preview=True)
            chunk, chunk_len = [], 0
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        await message.reply("\n".join(chunk), disable_web_page_preview=True)


@Client.on_message(
    filters.private & (
        filters.document | filters.video | filters.audio |
        filters.voice | filters.video_note | filters.animation |
        filters.sticker | filters.photo
    ),
    group=1
)
async def batch_file_interceptor(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id

    if cfg.MAINTENANCE_MODE and not is_admin(user_id):
        await message.reply(
            "🛠️ **Maintenance in progress.**\n"
            "The bot isn't accepting new files right now — please try again shortly."
        )
        return

    batch_id = batch_state.get_batch_id(user_id) or await db.get_user_active_batch(user_id)
    if not batch_id:
        return

    batch_state.set_batch(user_id, batch_id)

    info = _get_file_info(message)
    if not info or not DB_CHANNEL:
        return

    processing = await message.reply("⏳ Adding to batch…")

    try:
        try:
            fwd = await message.copy(DB_CHANNEL, caption=await build_uploader_caption(message, info["file_name"], info["file_size"], db=db))
        except Exception:
            # BUG FIX: see plugins/file_handler.py — a real Telegram-side
            # caption rejection (e.g. for a sticker) comes back as an
            # RPCError subclass, not ValueError, so the narrower except
            # this used to have never actually caught it. Retry without a
            # caption for any failure here; if THIS also fails, re-raise
            # so the real problem gets reported below.
            fwd = await message.copy(DB_CHANNEL)
    except Exception as e:
        logger.error("Batch forward error: %s", e)
        await processing.edit("❌ Could not save file. Make sure bot is admin in DB channel.")
        return

    file_uid = uuid.uuid4().hex[:12]
    await db.save_file(file_uid, {
        "file_id":     info["file_id"],
        "file_name":   info["file_name"],
        "file_size":   info["file_size"],
        "mime_type":   info["mime_type"],
        "type":        info["type"],
        "msg_id":      fwd.id,
        "uploader_id": user_id,
        "batch_id":    batch_id,
        # NOTE: no "saved_at" here — save_file() always sets it server-side
        # to datetime.utcnow(), so passing one here was silently discarded
        # on every single upload (dead work). See database.py's save_file.
    })
    await db.add_file_to_batch(batch_id, file_uid)
    await db.increment_stat("files_uploaded")
    await db.increment_stat("links_generated")

    batch_data = await db.get_batch(batch_id)
    count = len(batch_data.get("files", [])) if batch_data else "?"

    await processing.edit(
        f"✅ **Added to batch** ({count} files so far)\n"
        f"▸ `{info['file_name']}` · {humanbytes(info['file_size'])}\n\n"
        "Send more files or type /done to finish.",
        # count is "?" only if the batch lookup above unexpectedly came back
        # empty right after a successful add — fall back to 1 rather than 0
        # so the Remove Last button still shows (we know a file was added).
        reply_markup=_batch_progress_markup(batch_id, file_count=count if isinstance(count, int) else 1)
    )
    message.stop_propagation()
