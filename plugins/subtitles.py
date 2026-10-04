"""
/sub — attach a subtitle file to a video link that already exists.

The FFmpeg pipeline (web/media.py) can only reach streams that are *inside*
the container. That covers a lot, but not the common cases where a subtitle
arrives from elsewhere: a fan translation, a forced-subtitles track for the
foreign-language bits, a corrected .srt, or subs for a video uploaded before
this server had FFmpeg. Until now the only way in was to re-upload the video
with the subtitle next to it in a batch — which mints a brand-new link and
breaks every copy of the old one already in circulation.

/sub keeps the link. It's a two-step flow:

    /sub <file ID> [Label]     →  the bot says what it wants
    send the .srt / .ass / .vtt →  stored, converted, attached

The subtitle becomes a normal file record (so it is really stored, not
referenced from the user's chat) and the video's `media.subtitles` manifest
gains an entry pointing at it. Indices for attached tracks come from the
200-255 band reserved in database.attach_subtitle(), so they can never collide
with FFmpeg-extracted (0-99) or batch-sidecar (100-199) tracks.

Conversion happens here, at attach time, rather than lazily on the first page
view. Two reasons: the file is already in hand, so the work is free, and a
subtitle that parses to zero cues is worth telling the user about *now*
instead of leaving them a track that looks selected but shows nothing.
"""

import logging
import os
import uuid
from datetime import datetime, timedelta

from pyrogram import Client, filters
from pyrogram.types import Message

import info as cfg
from info import DB_CHANNEL
from utils import (
    check_force_sub, humanbytes, is_admin, is_expired, is_video_media,
    build_uploader_caption, expiry_datetime, md_escape,
    extract_file_info as _get_file_info,
)
import batch_state
import sub_state

logger = logging.getLogger(__name__)

# Subtitle bodies are small; anything over this is not a subtitle and was
# probably sent by mistake (a raw video renamed to .srt, say).
MAX_SUBTITLE_BYTES = 8 * 1024 * 1024


def _page_url(file_uid: str) -> str:
    return f"{cfg.URL.rstrip('/')}/file/{file_uid}" if cfg.URL else ""


def _decode_subtitle(raw: bytes) -> str:
    """
    Decode a subtitle body, guessing the encoding the way a player does.

    .srt files are written by whatever text editor whoever made them had open,
    so Windows-1252 is at least as common as UTF-8 — and French or Spanish
    subtitles are full of accented characters that turn into mojibake (or,
    decoded strictly, raise) if UTF-8 is assumed. Every one of these ends with
    a lossy fallback, because a handful of undecodable bytes should not cost
    the user the whole file.
    """
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


# ── /sub <file ID> [Label] ───────────────────────────────────────────────────

@Client.on_message(filters.command("sub") & filters.private)
async def sub_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id
    args = message.command

    if cfg.MAINTENANCE_MODE and not is_admin(user_id):
        await message.reply(
            "🛠️ **Maintenance in progress.**\n"
            "The bot isn't storing new files right now, so a subtitle can't be "
            "attached either. Try again shortly."
        )
        return

    if not DB_CHANNEL:
        await message.reply(
            "⚠️ **Bot not configured.**\n"
            "The admin hasn't set a DB Channel yet — run `/setup` or `/settings`."
        )
        return

    if not await check_force_sub(client, message):
        return

    if len(args) < 2:
        await message.reply(
            "**Usage:** `/sub <file ID> [Label]`\n\n"
            "Attach a subtitle file to a video link you already made.\n\n"
            "**1.** Find the file ID — the last part of its link, or with /mylinks\n"
            "**2.** Run it, e.g. `/sub 4c06e7b21fb5 English`\n"
            f"**3.** Send the `.srt`, `.ass` or `.vtt` file within "
            f"{sub_state.PENDING_TTL_SECONDS // 60} minutes\n\n"
            "The link doesn't change — anyone who already has it gets the "
            "subtitle. `/cancel` drops the request."
        )
        return

    # message.command is [name, ...args] (pyrogram/filters.py:797), so the file
    # ID is [1] — using [0] looked up a file literally called "sub".
    target_uid = args[1].strip()
    label = " ".join(args[2:]).strip()[:60]

    # /sub intercepts the next subtitle file, which would otherwise be claimed
    # as a new batch member — so the two flows aren't allowed to overlap.
    batch_id = batch_state.get_batch_id(user_id) or await db.get_user_active_batch(user_id)
    if batch_id:
        await message.reply(
            "⚠️ **You have an active batch** (`" + md_escape(batch_id) + "`).\n\n"
            "Finish it with /done, or /cancel to discard it, then run /sub "
            "again — otherwise the subtitle would be added as another file "
            "instead of being attached to your video."
        )
        return

    target = await db.get_file(target_uid)
    if not target:
        await message.reply(
            f"❌ No file found with ID `{md_escape(target_uid)}`.\n\n"
            "The ID is the last part of the link "
            "(`…/file/4c06e7b21fb5`), or see everything you've uploaded with "
            "/mylinks."
        )
        return

    if is_expired(target):
        await message.reply(
            "⏰ **That link has already expired**, so there's nothing to attach "
            "a subtitle to. The file itself may still be in storage, but the "
            "link is gone — upload it again to make a new one."
        )
        return

    if not is_video_media(
        target.get("type", ""),
        mime_type=target.get("mime_type"),
        file_name=target.get("file_name"),
    ):
        await message.reply(
            f"ℹ️ **`{md_escape(str(target.get('file_name') or 'That file'))}` "
            "isn't a video**, so a subtitle has nowhere to play.\n\n"
            "Subtitles can only be attached to video links."
        )
        return

    owner = target.get("uploader_id")
    if owner != user_id and not is_admin(user_id):
        await message.reply(
            "⛔ **Not your video.** You can only add subtitles to links you "
            "generated yourself — check them with /mylinks."
        )
        return

    replaced = sub_state.is_pending(user_id)
    sub_state.set_pending(user_id, target_uid, label)
    await message.reply(
        ("🔁 **Changed your mind — new target.**\n\n" if replaced else
         "📝 **Subtitle request open.**\n\n")
        + f"🎬 **Video:** `{md_escape(str(target.get('file_name') or target_uid))}`\n\n"
        "Now send the subtitle file — `.srt`, `.ass`, `.ssa` or `.vtt`.\n"
        f"You have {sub_state.PENDING_TTL_SECONDS // 60} minutes. "
        "Any other file you send is treated as a normal upload.\n"
        "Type /cancel to drop this request."
    )


# ── Capture: the next subtitle file ─────────────────────────────────────────

@Client.on_message(filters.private & filters.document, group=2)
async def sub_capture(client: Client, message: Message):
    """
    Store the subtitle the open /sub request is waiting for and attach it.

    Handler group 2, so it only ever sees a message that both upload paths
    already stood aside for — plugins/file_handler.py (group 0) and
    plugins/batch.py (group 1) each call sub_state.holds_upload() and return
    for exactly the subtitle-shaped filenames this one accepts.
    """
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id

    pending = sub_state.get_pending(user_id)
    if not pending:
        return
    video_uid, label, _language = pending

    info = _get_file_info(message)
    if not info:
        return

    # /sub refuses to start during maintenance, but the pause can begin while a
    # request is already open — and without this check the queued subtitle is
    # still copied into the storage channel behind it. Both upload paths do
    # re-check on every file; this is the third one.
    if cfg.MAINTENANCE_MODE and not is_admin(user_id):
        sub_state.clear_pending(user_id)
        await message.reply(
            "🛠️ **Maintenance in progress.**\n\n"
            "Nothing was attached and the /sub request is dropped — run it "
            "again once the bot is back."
        )
        # A bare return is enough: nothing in a lower group claims a document
        # (auto_help, the only later text handler, filters on filters.text).
        return

    # Imported lazily: web/__init__ imports utils, so a module-level import
    # back the other way would be a cycle.
    from web import media

    kind = media.has_sidecar_subtitle(info["file_name"])
    if not kind:
        # Unreachable through the groups above, which stand aside on this same
        # test — but if the two ever disagree, losing a subtitle is worse than
        # losing a frame, so the request is left open rather than consumed.
        return

    if info["file_size"] and info["file_size"] > MAX_SUBTITLE_BYTES:
        sub_state.clear_pending(user_id)
        await message.reply(
            f"⚠️ **{humanbytes(info['file_size'])} is too big for a subtitle.**\n\n"
            "`" + md_escape(info["file_name"]) + "` doesn't look like a "
            "subtitle file, so nothing was attached. Send the actual `.srt` "
            "with /sub again."
        )
        return

    target = await db.get_file(video_uid)
    if not target:
        sub_state.clear_pending(user_id)
        await message.reply(
            "❌ **The video for this request is gone** — it was deleted while "
            "the subtitle request was open, so there's nothing to attach to."
        )
        return

    processing = await message.reply("⏳ **Saving subtitle…**")

    try:
        caption = await build_uploader_caption(
            message, info["file_name"], info["file_size"], db=db)
        try:
            fwd = await message.copy(DB_CHANNEL, caption=caption)
        except Exception:
            # A real caption rejection (RPCError, not ValueError) — same
            # fallback the upload paths use; see plugins/batch.py.
            fwd = await message.copy(DB_CHANNEL)
        raw = await client.download_media(fwd, in_memory=True)
    except Exception as e:
        logger.error("Subtitle store failed for %s: %s", video_uid, e)
        sub_state.clear_pending(user_id)
        await processing.edit(
            "❌ **Could not save the subtitle.**\n\n"
            "▸ Make sure the bot is **admin** in the storage channel\n"
            "▸ Nothing was attached — your video link is unchanged"
        )
        return

    if not raw:
        sub_state.clear_pending(user_id)
        await processing.edit("❌ **The subtitle file came back empty.** Try sending it again with /sub.")
        return
    raw = bytes(raw.getbuffer())

    vtt = media.sidecar_to_vtt(_decode_subtitle(raw), kind)
    if vtt.count("-->") == 0:
        # Stored it, converted it, found nothing worth showing. Roll the
        # upload back — a dead record in the storage channel is worse than a
        # rejected one, and the user needs to hear this now, not from a player
        # that renders an empty track.
        try:
            await client.delete_messages(DB_CHANNEL, fwd.id)
        except Exception as e:
            # The copy is junk but deleting it can legitimately fail (the bot
            # lost channel admin rights); it must not mask the real message
            # below, which is that the subtitle had no readable cues.
            logger.warning("Could not delete rejected subtitle %s: %s", fwd.id, e)
        sub_state.clear_pending(user_id)
        await processing.edit(
            "⚠️ **No cues found in `" + md_escape(info["file_name"]) + "`**\n\n"
            "The file converted cleanly but contains no readable subtitle "
            "lines, so nothing was attached — it's likely a different format "
            "than its extension claims (an XML/YTT/PSB download, say). Your "
            "video link is unchanged."
        )
        return

    sub_uid = uuid.uuid4().hex[:12]
    file_data = {
        "file_id":     info["file_id"],
        "file_name":   info["file_name"],
        "file_size":   info["file_size"],
        "mime_type":   info["mime_type"],
        "type":        info["type"],
        "msg_id":      fwd.id,
        "uploader_id": user_id,
    }
    # The subtitle lasts exactly as long as the video it belongs to, and never
    # less: inheriting the target's expiry (rather than applying
    # LINK_EXPIRY_DAYS from today) is what stops a track from dying mid-life
    # and leaving the player pointing at a 404.
    target_expiry = expiry_datetime(target)
    if target_expiry:
        file_data["expires_at"] = target_expiry
    elif cfg.LINK_EXPIRY_DAYS:
        file_data["expires_at"] = datetime.utcnow() + timedelta(days=cfg.LINK_EXPIRY_DAYS)

    await db.save_file(sub_uid, file_data)
    # files_uploaded counts stored files; this is one. links_generated is not
    # incremented — no shareable link is minted for the subtitle itself.
    await db.increment_stat("files_uploaded")

    language = media.language_from_name(info["file_name"])
    # Explicit /sub label wins, then a language read off the filename
    # ("Movie.hindi.srt"), then the filename itself.
    track_label = (label or media.language_name(language)
                   or _stem(info["file_name"]))
    existing = (target.get("media") or {}).get("subtitles") or []
    idx = await db.attach_subtitle(
        video_uid, sub_uid,
        label=track_label,
        language=language,
        # First subtitle on a video that had none: turn it on by default, since
        # a bare <track> element still needs one checked entry to be usable.
        default=not existing,
    )
    if idx is None:
        await db.delete_file(sub_uid)
        sub_state.clear_pending(user_id)
        await processing.edit(
            "⚠️ **This video already has the maximum number of subtitle "
            "tracks**, so the new one wasn't attached. It is stored, and its "
            f"own link still works: {_page_url(sub_uid)}"
        )
        return

    # Cache the converted WebVTT under the name subtitle_handler() already
    # looks for, so the first page view is a file read instead of a Telegram
    # download plus a parse.
    try:
        cached = media.track_dir(video_uid) / f"sc{idx}.vtt"
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(vtt, encoding="utf-8")
    except Exception as e:
        # Not fatal: the route falls back to converting on demand.
        logger.warning("Could not cache converted subtitle %s: %s", sub_uid, e)

    sub_state.clear_pending(user_id)
    cue_count = vtt.count("-->")
    page = _page_url(video_uid)
    await processing.edit(
        "✅ **Subtitle attached.**\n\n"
        f"🎬 `{md_escape(str(target.get('file_name') or video_uid))}`\n"
        f"💬 **Track {idx}:** {md_escape(track_label)}\n"
        f"📝 {cue_count} cue{'s' if cue_count != 1 else ''} · "
        f"{humanbytes(info['file_size'])}"
        + (f"\n\n🌐 **Web Page:** `{page}`" if page else "")
        + ("\n\nThe link itself didn't change — anyone who already has it now "
           "gets the subtitle. Pick it under **Subtitles** in the player."
           if page else
           "\n\nOpen the file's web page and choose it in the player's "
           "subtitle menu. Set a site URL with /settings to get a page link "
           "here.")
    )


def _stem(file_name: str) -> str:
    """Fallback track label from the subtitle's own filename."""
    return (os.path.splitext(os.path.basename(file_name or ""))[0] or "Subtitles")[:40]


# ── /unsub <file ID> [track] ─────────────────────────────────────────────────

@Client.on_message(filters.command("unsub") & filters.private)
async def unsub_command(client: Client, message: Message):
    """
    Remove a subtitle that /sub attached.

    Only the 200+ band is detachable, and database.detach_subtitle() explains
    why: an FFmpeg-extracted track is part of the video itself, so dropping its
    manifest entry just hides it until the next probe puts it straight back
    while the remuxed audio stays on disk, and a batch sidecar is a real batch
    member in its own right.

    The track can be named by index (from the list) or by label, since
    "which one was 201?" is not something anyone remembers.
    """
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id
    args = message.command

    if len(args) < 2:
        await message.reply(
            "**Usage:** `/unsub <file ID> [track]`\n\n"
            "Removes a subtitle you added with /sub.\n\n"
            "**Example:** `/unsub 4c06e7b21fb5 200` or "
            "`/unsub 4c06e7b21fb5 English`\n\n"
            "Run it with just the file ID to list that video's removable "
            "tracks. Tracks baked into the video itself (extracted by FFmpeg) "
            "and subtitles uploaded next to it in a batch aren't listed — "
            "those belong to the file, not to /sub."
        )
        return

    file_uid = args[1].strip()
    meta = await db.get_file(file_uid)
    if not meta:
        await message.reply(f"❌ No file found with ID `{md_escape(file_uid)}`.")
        return

    owner = meta.get("uploader_id")
    if owner != user_id and not is_admin(user_id):
        await message.reply(
            "⛔ **Not your video.** You can only change subtitles on links you "
            "generated yourself — see them with /mylinks."
        )
        return

    detachable = [s for s in (meta.get("media") or {}).get("subtitles", [])
                  if (s.get("index") or 0) >= db._ATTACHED_SUB_BASE]
    others = len((meta.get("media") or {}).get("subtitles", [])) - len(detachable)

    def _listing() -> str:
        if not detachable:
            return ("📭 **No /sub subtitles on this video** "
                    "to remove.\n\nTracks that came from inside the file, or "
                    "from a `.srt` uploaded alongside it in a batch, stay with "
                    "the file — add one with /sub if you meant to replace it.")
        rows = [f"• `{s.get('index')}` — **{md_escape(str(s.get('label') or 'Subtitles'))}**"
                + (" · default" if s.get("default") else "")
                for s in detachable]
        note = (f"\n\n{others} other track{'s' if others != 1 else ''} in the "
                "file itself can't be removed here." if others else "")
        return ("💬 **Removable tracks:**\n" + "\n".join(rows)
                + f"\n\n`/unsub {md_escape(file_uid)} <track>`" + note)

    if len(args) < 3:
        await message.reply(_listing())
        return

    want = " ".join(args[2:]).strip()
    if want.isdigit():
        index, matches = int(want), [s for s in detachable if s.get("index") == int(want)]
    else:
        index, matches = None, [
            s for s in detachable
            if (s.get("label") or "").strip().lower() == want.lower()
            or (s.get("language") or "").strip().lower() == want.lower()
        ]

    if len(matches) != 1:
        reason = ("no subtitle matches that" if not matches
                  else "several tracks share that name")
        await message.reply(
            f"⚠️ Couldn't remove `{md_escape(want)}` — {reason}.\n\n" + _listing()
        )
        return
    index = matches[0].get("index")

    removed, src_uid = await db.detach_subtitle(file_uid, index)
    if not removed:
        await message.reply(_listing())
        return

    label = str(matches[0].get("label") or f"Track {index}")
    # The subtitle file exists only to be this track: leaving its record behind
    # puts an orphan row in the library (and a stray page link) that nothing
    # points at any more.
    if src_uid and not await db.delete_file(src_uid):
        logger.info("Subtitle record %s was already gone", src_uid)

    from web import media
    if src_uid:
        # Subtitles get no track directory of their own, but a stale cache is
        # cheap to rule out and this is the one path that had them.
        media.cleanup_dir(src_uid)
    # This is the exact path subtitle_handler() looks for first; if it survived,
    # the removed track would keep playing from cache.
    try:
        (media.track_dir(file_uid) / f"sc{index}.vtt").unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Could not purge cached VTT for %s/%s: %s", file_uid, index, e)

    left = len(detachable) - 1
    await message.reply(
        f"✅ **Removed {md_escape(label)}** (track `{index}`).\n"
        f"🎬 `{md_escape(str(meta.get('file_name') or file_uid))}` "
        f"has {left} /sub subtitle{'s' if left != 1 else ''} left."
    )
