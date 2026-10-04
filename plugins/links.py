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
from pyrogram.types import Message

import info as cfg
from utils import delete_file_completely, humanbytes, is_admin, md_escape

logger = logging.getLogger(__name__)

# How many recent uploads /mylinks lists. Chosen to fit comfortably in one
# Telegram message (4096-char cap) once each row is a name + two links, and
# to be a useful "show me what I've been doing" window without paging.
MYLINKS_LIMIT = 10


def _page_url(file_uid: str) -> str:
    return f"{cfg.URL.rstrip('/')}/file/{file_uid}" if cfg.URL else ""


def _download_url(file_uid: str) -> str:
    return f"{cfg.URL.rstrip('/')}/download/{file_uid}" if cfg.URL else ""


# ── /mylinks ─────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("mylinks") & filters.private)
async def mylinks_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id

    files = await db.get_files_by_uploader(user_id, MYLINKS_LIMIT)
    if not files:
        await message.reply(
            "📭 **You haven't uploaded anything yet.**\n\n"
            "Send me a file and I'll turn it into a shareable link — "
            "everything you make will show up here so you can find it again."
        )
        return

    if not cfg.URL:
        await message.reply(
            "⚠️ **Web links aren't configured (no `URL` set),** so there's "
            "nothing to list here yet. The files were saved fine — an admin "
            "just needs to set the site URL first."
        )
        return

    lines = [
        f"🔗 **Your {len(files)} most recent link{'s' if len(files) != 1 else ''}**\n"
    ]
    for i, f in enumerate(files, 1):
        uid = f.get("_id") or f.get("file_uid") or "?"
        name = md_escape(str(f.get("file_name") or "Unnamed file"))[:60]
        size = humanbytes(int(f.get("file_size") or 0))
        # 🔒 marks a link that now asks for a password, so the list doubles as
        # the answer to "which of these did I put a lock on?"
        mark = "🔒 " if f.get("lock") else ""
        lines.append(f"**{i}.** {mark}{name} · `{size}`")
        lines.append(f"   [Open page]({_page_url(uid)}) · [Download]({_download_url(uid)})")

    lines.append(
        "\n⁉️ Wrong name on one of these? `/rename <file ID> <new name>` — "
        "the ID is the last part of its link."
    )
    await message.reply("\n".join(lines), disable_web_page_preview=True)


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
    file_uid = args[1]
    new_name = " ".join(args[2:]).strip()

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
        await message.reply(f"❌ No file found with ID `{md_escape(file_uid)}`.")
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
            f"\n\n⚠️ The extension changed (`.{old_ext}` → `.{new_ext}`), so "
            "the browser may now treat it as a different media type. Rename "
            "it back if that wasn't intended."
        )

    who = "" if owner == user_id else " (as admin)"
    await message.reply(
        f"✅ **Renamed{who}.**\n"
        f"~{md_escape(str(old_name))[:80]}~\n"
        f"**{md_escape(new_name[:80])}**\n\n"
        f"🔗 Link unchanged: {_page_url(file_uid)}{warn}"
    )


# ── /unlink ──────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("unlink") & filters.private)
async def unlink_command(client: Client, message: Message):
    """
    Take one of your own links down.

    /delete always refused non-admins, so a user who had shared a link
    widely — a group post, a forum thread — had no way to retract it, and had
    to go ask an admin to delete a file that was theirs. This is the
    uploader's side of that, with the same ownership rule as /rename.

    The file record goes, which is what makes the link stop working; the copy
    in the storage channel is left alone (deleting it would need the bot's
    message history in that channel, and the link is dead either way).
    """
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id
    args = message.command

    if len(args) < 2:
        await message.reply(
            "**Usage:** `/unlink <file ID>`\n\n"
            "**Example:** `/unlink 4c06e7b21fb5`\n\n"
            "Removes one of your own links — it stops working immediately, for "
            "everyone who has it. Find the ID with /mylinks (it's the last part "
            "of the link). This can't be undone: the file has to be uploaded "
            "again, which gives a new link."
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
            "⛔ **Not your file.** You can only remove links you generated "
            "yourself — see them with /mylinks."
        )
        return

    name = md_escape(str(meta.get("file_name") or file_uid))[:80]
    deleted, removed_subs = await delete_file_completely(db, file_uid)
    if not deleted:
        await message.reply(f"❌ Couldn't remove `{md_escape(file_uid)}` — try again.")
        return

    who = "" if owner == user_id else " (as admin)"
    subs = (f"\n🧹 Removed {removed_subs} attached "
            f"subtitle file{'s' if removed_subs != 1 else ''} with it."
            if removed_subs else "")
    await message.reply(
        f"✅ **Link removed{who}.**\n"
        f"🗑 `{name}` is gone from the library, and "
        f"`…/file/{md_escape(file_uid)}` no longer works for anyone.{subs}"
    )
