"""
/privacy — lets any user hide their own identity (name, username, user ID)
from the "Uploaded by" block shown on their files' storage captions (see
utils.build_uploader_caption). This is entirely opt-in and user-controlled.

Important: this ONLY affects what's displayed in that caption text. It does
NOT hide anything from admins — /userinfo and other admin tools always read
the real stored identity from the users collection regardless of this
setting. This is a display-privacy toggle, not an anonymity/security
feature, and the command explains that plainly so nobody mistakes it for
more than it is.
"""

from pyrogram import Client, filters
from pyrogram.types import Message


@Client.on_message(filters.command("privacy") & filters.private)
async def privacy_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id

    args = message.command
    arg = args[1].strip().lower() if len(args) > 1 else ""

    if arg not in ("on", "off"):
        info_doc = await db.get_user_info(user_id)
        current = bool(info_doc and info_doc.get("privacy_mode"))
        status = "🔒 **ON** — your identity is hidden" if current else "👤 **OFF** — your identity is shown normally"
        await message.reply(
            f"**Privacy mode is currently:** {status}\n\n"
            "When ON, files you upload show as anonymous in the storage "
            "channel's \"Uploaded by\" info — instead of your name, "
            "username, and user ID, others just see 🔒 Anonymous.\n\n"
            "This only changes what's shown there. Admins can always see "
            "your real account info through their own tools — this isn't "
            "a way to hide from moderation, just from that one caption "
            "text.\n\n"
            "**Usage:** `/privacy on` or `/privacy off`"
        )
        return

    enabled = (arg == "on")
    await db.set_privacy_mode(user_id, enabled)

    if enabled:
        await message.reply(
            "🔒 **Privacy mode enabled.**\n\n"
            "From now on, your uploads will show as anonymous in the "
            "\"Uploaded by\" caption instead of your real name/username/"
            "ID. This doesn't apply retroactively to files you've already "
            "uploaded — only new ones going forward.\n\n"
            "Admins can still see your real account details through their "
            "own tools; this only affects that one caption text."
        )
    else:
        await message.reply(
            "👤 **Privacy mode disabled.**\n\n"
            "Your uploads will show your real name/username/ID in the "
            "\"Uploaded by\" caption again, going forward."
        )
