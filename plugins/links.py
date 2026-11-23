"""
Link management commands — /mylinks, /rename and /unlink.

These fill gaps that the rest of the bot leaves open:

  • /mylinks — the only file listing that existed was the admin-only
    /recentfiles, so a normal user who lost track of a link they'd generated
    (the single most common thing to want on a link bot) had no way to get it
    back. /mylinks lists *your own* recent uploads with live links, pulled
    from the uploader_id index that's been there all along.

  • /rename — a file's name is chosen by whoever uploaded it and is otherwise
    permanent: it's what the web page title, the Content-Disposition download
    name, and the whole library show. A typo ("Final-final-v3.mkv") or an
    ugly Telegram-forwarded name used to be unfixable without re-uploading
    (which mints a brand-new link). /rename edits just the name and keeps the
    link working for everyone who already has it.

  • /unlink — /delete always refused anyone but an admin, so a user who had
    already shared a link around had no way to take it back down and had to go
    ask someone to delete a file that was theirs. /unlink is the uploader's
    side of that.

All three act on the uploader's own files; admins can act on anyone's (a user
who's lost access to the account they uploaded from, or a name that needs
cleaning up for compliance, is the realistic case for that).
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import info as cfg
from utils import (
    delete_file_completely, expiry_datetime, humanbytes, is_admin, is_expired,
    md_escape, parse_file_uid,
)

logger = logging.getLogger(__name__)

# How many uploads /mylinks lists per page. Chosen to fit comfortably in one
# Telegram message (4096-char cap) once each row is a name + two links.
# Older ones are reached with /mylinks 2, /mylinks 3, ...
MYLINKS_LIMIT = 10


def _clip(text: str, limit: int) -> str:
    """Shorten FIRST, then escape. Escaping first and slicing after could cut
    an escape sequence in half and leave a stray backslash that swallows the
    next character."""
    text = str(text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return md_escape(text)


def _code(text: str) -> str:
    """Text safe to put inside `inline code` (a backtick would end it)."""
    return str(text).replace("`", "'")[:60]


def _page_url(file_uid: str) -> str:
    return f"{cfg.URL.rstrip('/')}/file/{file_uid}" if cfg.URL else ""


def _download_url(file_uid: str) -> str:
    return f"{cfg.URL.rstrip('/')}/download/{file_uid}" if cfg.URL else ""


# ── /mylinks ─────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("mylinks") & filters.private)
async def mylinks_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id

    args = message.command
    page = 1
    if len(args) > 1:
        if args[1].isdigit() and int(args[1]) >= 1:
            page = int(args[1])
        else:
            await message.reply(
                "**Usage:** `/mylinks` or `/mylinks <page number>`, "
                "e.g. `/mylinks 2` for older links."
            )
            return

    total = await db.count_files_by_uploader(user_id)
    if total == 0:
        await message.reply(
            "📭 **You haven't uploaded anything yet.**\n\n"
            "Send me a file and I'll turn it into a shareable link — "
            "everything you make will show up here so you can find it again."
        )
        return

    pages = -(-total // MYLINKS_LIMIT)
    if page > pages:
        await message.reply(
            f"📄 You only have **{pages}** page{'s' if pages != 1 else ''} "
            f"of links ({total} file{'s' if total != 1 else ''}). "
            f"Try `/mylinks {pages}`."
        )
        return

    if not cfg.URL:
        await message.reply(
            "⚠️ **Web links aren't configured (no `URL` set),** so there's "
            "nothing to list here yet. The files were saved fine — an admin "
            "just needs to set the site URL first."
        )
        return

    skip = (page - 1) * MYLINKS_LIMIT
    files = await db.get_files_by_uploader(user_id, MYLINKS_LIMIT, skip=skip)

    if pages == 1:
        title = f"🔗 **Your {total} link{'s' if total != 1 else ''}**\n"
    else:
        title = (f"🔗 **Your links** — {skip + 1}–{skip + len(files)} "
                 f"of {total}\n")
    lines = [title]
    for i, f in enumerate(files, skip + 1):
        uid = f.get("_id") or f.get("file_uid") or "?"
        name = _clip(f.get("file_name") or "Unnamed file", 60)
        size = humanbytes(int(f.get("file_size") or 0))
        # 🔒 marks a link that now asks for a password, so the list doubles as
        # the answer to "which of these did I put a lock on?"
        mark = "🔒 " if f.get("lock") else ""
        lines.append(f"**{i}.** {mark}{name} · `{size}`")
        lines.append(f"   [Open page]({_page_url(uid)}) · [Download]({_download_url(uid)}) · `{uid}`")

    if page < pages:
        lines.append(f"\n📄 Page {page}/{pages} — `/mylinks {page + 1}` for older links.")
    lines.append(
        "\n🔗 Get a link's buttons again: `/link <ID>` · "
        "wrong name? `/rename <ID or link> <new name>`."
    )
    await message.reply("\n".join(lines), disable_web_page_preview=True)


# ── /link ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command(["link", "getlink"]) & filters.private)
async def link_command(client: Client, message: Message):
    """Re-send the link card (links + buttons) for one of your files.

    Before this, the only way to see a link again was to scroll back through
    the chat or read it out of /mylinks as plain text with no buttons.
    `/link` alone gives your most recent upload; `/link <ID or link>` any of
    your files (admins: anyone's).
    """
    user = message.from_user
    if not user:
        return
    db = client.db  # type: ignore[attr-defined]
    args = message.command

    if len(args) >= 2:
        uid = parse_file_uid(args[1])
        meta = await db.get_file(uid) if uid else None
    else:
        recent = await db.get_files_by_uploader(user.id, 1, skip=0)
        meta = recent[0] if recent else None
        uid = (meta or {}).get("_id") or (meta or {}).get("file_uid") or ""
        if not meta:
            await message.reply(
                "📭 **You haven't uploaded anything yet.**\n\n"
                "Send me a file and I'll make a link for it."
            )
            return

    if not meta or (meta.get("uploader_id") != user.id and not is_admin(user.id)):
        # Same answer for "missing" and "not yours" — don't confirm that
        # someone else's ID exists.
        await message.reply(
            "❌ **I couldn't find that file among your links.**\n\n"
            "Use `/link` for your latest upload, `/link <ID>` for another one, "
            "or see everything with /mylinks."
        )
        return
    if is_expired(meta):
        await message.reply("⏰ **That link has expired.** Send the file again for a new one.")
        return
    if not cfg.URL:
        await message.reply(
            "⚠️ **Web links aren't configured yet.** Ask the bot admin to set "
            "the site URL in /settings."
        )
        return

    from plugins.file_handler import build_link_card
    text, markup = build_link_card(
        uid,
        name=meta.get("file_name") or "file",
        size=int(meta.get("file_size") or 0),
        mime=meta.get("mime_type") or "application/octet-stream",
        ftype=meta.get("type") or "document",
        expires_at=expiry_datetime(meta),
    )
    if meta.get("lock"):
        text += "\n\n🔒 Password protected — share the password separately."
    await message.reply(text, reply_markup=markup, disable_web_page_preview=True)


# ── /rename ──────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("rename") & filters.private)
async def rename_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id
    args = message.command

    if len(args) < 2:
        await message.reply(
            "**Usage:** `/rename <file ID> <new name>`\n\n"
            "**Example:** `/rename 4c06e7b21fb5 My Holiday Movie.mkv`\n\n"
            "Find the file ID at the end of its link (or with /mylinks). "
            "You can rename a file you uploaded; admins can rename any file. "
            "The link itself doesn't change — everyone who already has it "
            "just sees the new name."
        )
        return

    # message.command is [name, ...args] (pyrogram/filters.py:797): the file ID
    # is args[1], and everything after it is the new name.
    # Take the name from the raw text: message.command re-tokenises quoted
    # words and collapses spacing, which quietly altered names the user typed.
    parts = (message.text or "").split(None, 2)
    file_uid = parse_file_uid(parts[1] if len(parts) > 1 else "")
    new_name = parts[2].strip() if len(parts) > 2 else ""
    if not new_name:
        await message.reply(
            "✏️ Tell me the new name too:\n"
            "`/rename <file ID or link> <new name>`"
        )
        return

    # Sanitize before storing. The name is later reflected into an HTML page
    # title and a Content-Disposition header, so control characters, quotes,
    # slashes and path separators have to go regardless of who typed this.
    # (web/app.py also strips slashes when it builds the header, but a stored
    # name containing one is confusing enough to be worth normalising here.)
    new_name = "".join(c for c in new_name if c.isprintable() and c not in '/\\')
    new_name = new_name.replace('"', "'").replace("\u00a0", " ").strip()
    if len(new_name) > 200:
        new_name = new_name[:200].rstrip()
    if not new_name:
        await message.reply("⚠️ That name is empty once the illegal characters are removed. Try again.")
        return

    meta = await db.get_file(file_uid)
    if not meta:
        await message.reply(f"❌ No file found with ID `{_code(file_uid)}`.")
        return

    # Ownership: only the uploader may rename their own file, plus admins.
    owner = meta.get("uploader_id")
    if owner != user_id and not is_admin(user_id):
        await message.reply(
            "⛔ **Not your file.** You can only rename files you uploaded "
            "yourself (check them with /mylinks)."
        )
        return

    old_name = meta.get("file_name") or "(unnamed)"
    if old_name == new_name:
        await message.reply("ℹ️ That's already the file's name — nothing changed.")
        return

    await db.rename_file(file_uid, new_name)

    # A rename that changes the extension changes how the file is served:
    # web/app.py re-detects the MIME type from the stored name, so renaming
    # "clip.mp4" to "clip.jpg" would make the stream endpoint answer with the
    # wrong Content-Type. Warn rather than block — the user usually means it,
    # and blocking on a difference they can't see would be worse.
    old_ext = old_name.rsplit(".", 1)[-1].lower() if "." in old_name else ""
    new_ext = new_name.rsplit(".", 1)[-1].lower() if "." in new_name else ""
    warn = ""
    if old_ext != new_ext:
        warn = (
            f"\n\n⚠️ The extension changed (`.{_code(old_ext)}` → `.{_code(new_ext)}`), so "
            "the browser may now treat it as a different media type. Rename "
            "it back if that wasn't intended."
        )

    who = "" if owner == user_id else " (as admin)"
    await message.reply(
        f"✅ **Renamed{who}.**\n"
        f"~~{_clip(old_name, 80)}~~\n"
        f"**{_clip(new_name, 80)}**\n\n"
        + (f"🔗 Link unchanged: {_page_url(file_uid)}" if cfg.URL
           else "🔗 The link is unchanged.")
        + warn
    )


# ── /unlink ──────────────────────────────────────────────────────────────────

async def _unlink_now(db, user_id: int, file_uid: str) -> str:
    """
    Remove a link and return the message to show. The ownership check lives
    HERE, not in the callers, so it runs again at the moment the confirmation
    button is pressed rather than being trusted from when it was shown.

    The file record goes, which is what makes the link stop working; the copy
    in the storage channel is left alone (deleting it would need the bot's
    message history in that channel, and the link is dead either way).
    """
    meta = await db.get_file(file_uid)
    if not meta:
        return "ℹ️ That link is already gone — nothing left to remove."

    owner = meta.get("uploader_id")
    if owner != user_id and not is_admin(user_id):
        return (
            "⛔ **Not your file.** You can only remove links you generated "
            "yourself — see them with /mylinks."
        )

    name = _code(meta.get("file_name") or file_uid)
    deleted, removed_subs = await delete_file_completely(db, file_uid)
    if not deleted:
        return f"❌ Couldn't remove `{_code(file_uid)}` — try again."

    who = "" if owner == user_id else " (as admin)"
    subs = (f"\n🧹 Removed {removed_subs} attached "
            f"subtitle file{'s' if removed_subs != 1 else ''} with it."
            if removed_subs else "")
    return (
        f"✅ **Link removed{who}.**\n"
        f"🗑 `{name}` is gone from the library, and "
        f"`…/file/{_code(file_uid)}` no longer works for anyone.{subs}"
    )


@Client.on_message(filters.command("unlink") & filters.private)
async def unlink_command(client: Client, message: Message):
    """
    Take one of your own links down - after asking first.

    /delete always refused non-admins, so a user who had shared a link widely
    had no way to retract it. This is the uploader's side of that, with the
    same ownership rule as /rename. Removing a link can't be undone and it
    breaks it for everyone who has it, so by default this shows the file and
    asks for a button press; `/unlink <id> yes` skips the question.
    """
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id
    args = message.command

    if len(args) < 2:
        await message.reply(
            "**Usage:** `/unlink <file ID or link>`\n\n"
            "Removes one of your own links — it stops working immediately, for "
            "everyone who has it. Paste the link or its ID (see /mylinks). "
            "I'll ask you to confirm first. This can't be undone: the file has "
            "to be uploaded again, which gives a new link.\n\n"
            "Already sure? `/unlink <file ID or link> yes` skips the question."
        )
        return

    file_uid = parse_file_uid(args[1])
    meta = await db.get_file(file_uid)
    if not meta:
        await message.reply(f"❌ No file found with ID `{_code(file_uid)}`.")
        return

    if meta.get("uploader_id") != user_id and not is_admin(user_id):
        await message.reply(
            "⛔ **Not your file.** You can only remove links you generated "
            "yourself — see them with /mylinks."
        )
        return

    if len(args) >= 3 and args[2].lower() in ("yes", "confirm", "now", "y"):
        await message.reply(await _unlink_now(db, user_id, file_uid))
        return

    await message.reply(
        "🗑 **Remove this link?**\n\n"
        f"📄 {_clip(meta.get('file_name') or file_uid, 70)}\n"
        f"💾 {humanbytes(int(meta.get('file_size') or 0))}\n\n"
        "It will stop working **immediately for everyone who has it**, and "
        "this can't be undone.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Yes, remove it", callback_data=f"ul:y:{file_uid}"),
            InlineKeyboardButton("↩️ Keep it", callback_data=f"ul:n:{file_uid}"),
        ]]),
    )


@Client.on_callback_query(filters.regex(r"^ul:[yn]:"))
async def unlink_confirm_cb(client: Client, cq: CallbackQuery):
    _, action, file_uid = cq.data.split(":", 2)
    if action == "n":
        await cq.answer("Kept")
        text = "👍 Kept — the link still works."
    else:
        text = await _unlink_now(client.db, cq.from_user.id, file_uid)  # type: ignore[attr-defined]
        await cq.answer()
    try:
        await cq.message.edit_text(text)
    except Exception as e:
        logger.debug("unlink confirmation edit skipped: %s", e)
