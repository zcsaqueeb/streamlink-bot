"""
/batch — Collect multiple files and generate a single shareable batch link.

Usage:
  /batch     → start collecting files
  send files → each gets saved and added to the batch
  /done      → finalize and receive a single batch link
  /cancel    → cancel current batch
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
    check_force_sub, humanbytes, build_uploader_caption, is_admin,
    extract_file_info as _get_file_info, schedule_track_extraction,
)
import batch_state
import lock_state
import sub_state

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
    state = _setup._WIZARD_STATE.pop(user_id, None)
    if state is not None:
        # Popping the state alone isn't enough: the wizard records the admin's
        # own pasted answer in `cleanup_message_ids` precisely because a
        # /settings step can ask for a DB URI that contains credentials. The
        # button-driven cancel (setup_cancel_cb) deletes them; this path has to
        # match it, or /cancel leaks the secret in the chat forever.
        await _setup._cleanup_wizard_messages(client, message.chat.id, state)
        await message.reply("❌ **Setup/settings edit cancelled.** Nothing was changed.")
        return

    db = client.db  # type: ignore[attr-defined]

    # Same dead-end treatment for an open /sub request: without this, a user
    # who typed /sub and changed their mind had no way to release the hold on
    # their next upload except waiting out the TTL.
    if sub_state.is_pending(user_id):
        sub_state.clear_pending(user_id)
        await message.reply("❌ **Subtitle request cancelled.** Nothing was attached.")
        return

    # …and the same for an open /lock. There's no secret stored yet — only a
    # prompt message waiting for a password — but leaving it up means the next
    # plain message this user sends gets hashed as a password by surprise, so
    # /cancel has to be able to close the door properly. The prompt is deleted
    # with the flow so it isn't left hanging in the chat.
    if lock_state.is_pending(user_id):
        file_uid, chat_id, prompt_id = lock_state.get_pending(user_id)
        lock_state.clear_pending(user_id)
        if prompt_id:
            try:
                await client.delete_messages(chat_id or message.chat.id, prompt_id)
            except Exception as e:
                logger.debug("Could not delete the abandoned /lock prompt: %s", e)
        await message.reply("❌ **Password request cancelled.** No lock was set.")
        return

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


async def _require_batch_owner(db, batch_id: str, user_id: int) -> bool:
    """True if `user_id` created `batch_id` (or is an admin).

    SECURITY FIX: the batch callbacks below used to take batch_id straight
    out of callback_data and act on it — close the batch, remove its last
    file, finalize it and steal the resulting link — with no check that the
    person tapping belonged to that batch. Anyone who learned another user's
    batch id could destroy or claim their in-progress batch.
    """
    if is_admin(user_id):
        return True
    batch = await db.get_batch(batch_id)
    return bool(batch) and batch.get("creator_id") == user_id


@Client.on_callback_query(filters.regex(r"^batch_done_(.+)$"))
async def batch_done_cb(client, cq):
    batch_id = cq.data.split("_", 2)[2]
    db = client.db  # type: ignore[attr-defined]
    if not await _require_batch_owner(db, batch_id, cq.from_user.id):
        await cq.answer("⛔ This batch isn't yours.", show_alert=True)
        return
    await cq.answer()
    await _finalize_batch(client, cq.message, cq.from_user.id, batch_id=batch_id)


@Client.on_callback_query(filters.regex(r"^batch_cancel_(.+)$"))
async def batch_cancel_cb(client, cq):
    batch_id = cq.data.split("_", 2)[2]
    db = client.db  # type: ignore[attr-defined]
    if not await _require_batch_owner(db, batch_id, cq.from_user.id):
        await cq.answer("⛔ This batch isn't yours.", show_alert=True)
        return
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

    if not await _require_batch_owner(db, batch_id, cq.from_user.id):
        await cq.answer("⛔ This batch isn't yours.", show_alert=True)
        return

    removed_uid = await db.remove_last_file_from_batch(batch_id)
    if not removed_uid:
        await cq.answer("Nothing to remove — the batch is already empty.", show_alert=True)
        return

    # BUG FIX: removing a file deletes its DB record (see
    # remove_last_file_from_batch), but the counters incremented when it was
    # uploaded were never reversed — so every "oops" upload permanently
    # inflated the site-wide files_uploaded / links_generated totals shown by
    # /stats. increment_stat is a plain $inc, so a negative amount is the same
    # primitive the upload path used to add it.
    try:
        await db.increment_stat("files_uploaded", -1)
        await db.increment_stat("links_generated", -1)
    except Exception as e:
        logger.warning("Could not roll back stats after batch_undo %s: %s", removed_uid, e)

    # Extracted subtitle/audio caches are reclaimed by the hourly orphan
    # sweep anyway, but that's a whole track directory sitting on disk for up
    # to an hour over a file the user just said they don't want.
    try:
        from web import media
        media.cleanup_dir(removed_uid)
    except Exception as e:
        logger.warning("Track cleanup failed for %s: %s", removed_uid, e)

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

    # The batch stays OPEN and stays claimed by this user until its links are
    # actually in their hands. Closing here first meant that if any reply below
    # raised — a FloodWait, the /done message having been deleted, the chat
    # closed — the batch was already `closed`, so the next /done answered "No
    # active batch found" and the files were unreachable forever: stored, billed
    # to the stats, and with no way left to recover the batch id that the only
    # link to them is built from. Retrying /done is now always possible, and
    # re-sending a list the user already has is the far milder failure.
    batch_url = f"{URL}/batch/{bid}" if URL else None

    # IMPROVEMENT: when a web batch page is configured, it already shows
    # every file with thumbnails, search, and a download-all button — so
    # repeating the same info as a giant text list here was both redundant
    # AND risky: Telegram caps a single message at 4096 characters, and a
    # batch of 30+ files with long names could silently exceed that and
    # fail to send at all. Just point at the page instead.
    if batch_url:
        expiry_note = (
            f"\n⏳ Every link in this batch expires in **{cfg.LINK_EXPIRY_DAYS}** "
            "day(s), like all other links here."
            if cfg.LINK_EXPIRY_DAYS else ""
        )
        await message.reply(
            f"✅ **Batch Ready! ({len(files)} files)**\n\n"
            f"🔗 **Batch Link:** `{batch_url}`{expiry_note}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📦 Open Batch Page", url=batch_url)]]
            ),
            disable_web_page_preview=True,
        )
        await db.close_batch(bid)
        batch_state.clear_batch(user_id)
        return

    # No web URL configured — individual links are the ONLY way to reach
    # these files, so they must always be delivered in full. Rather than
    # risk one oversized message silently failing to send past Telegram's
    # 4096-char limit, build the list in chunks and send as multiple
    # messages when needed — every file's link always gets through.
    lines = []
    # PERFORMANCE: fetch every file record concurrently. This used to be a
    # sequential `await db.get_file()` inside the loop, so a 40-file batch
    # made 40 round trips back-to-back before the first line was even
    # built — the user watched nothing happen for that whole time.
    metas = await asyncio.gather(*(db.get_file(fuid) for fuid in files))
    for i, (fuid, fm) in enumerate(zip(files, metas), 1):
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

    await db.close_batch(bid)
    batch_state.clear_batch(user_id)


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

    # /batch checks the force-subscribe gate when it opens a batch, but a batch
    # can stay open for a long time and the admin's requirement is evaluated
    # *now*. Without this re-check, someone who joined the channel to start the
    # batch and left again (or whom the admin gated after they started) kept
    # collecting links from group 1 while every single upload was refused.
    if not await check_force_sub(client, message):
        return

    batch_state.set_batch(user_id, batch_id)

    info = _get_file_info(message)
    if not info or not DB_CHANNEL:
        return

    # An open /sub request owns the next subtitle file; batch collection must
    # not claim it as a new member. Same stand-aside as the single-upload path
    # (plugins/file_handler.py) so the two paths can't disagree about who gets
    # the message.
    if sub_state.holds_upload(user_id, info["file_name"]):
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
    file_data = {
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
    }
    # BUG FIX: batch uploads ignored LINK_EXPIRY_DAYS entirely, so an admin
    # who configured expiry got exactly that — single links expiring on
    # schedule while every batch link (and each file inside it) stayed live
    # forever. Same rule now applies to both upload paths.
    if cfg.LINK_EXPIRY_DAYS:
        file_data["expires_at"] = (
            datetime.utcnow() + timedelta(days=cfg.LINK_EXPIRY_DAYS)
        )
    await db.save_file(file_uid, file_data)
    await db.add_file_to_batch(batch_id, file_uid)
    await db.increment_stat("files_uploaded")
    await db.increment_stat("links_generated")

    # Same detached subtitle/audio extraction as the single-upload path
    # (plugins/file_handler.py) — videos only, no-ops without FFmpeg.
    schedule_track_extraction(client, db, file_uid, fwd.id, info)

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
