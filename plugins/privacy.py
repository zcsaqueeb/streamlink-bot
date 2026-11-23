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

_ON = {"on", "enable", "enabled", "yes", "y", "true", "1", "hide"}
_OFF = {"off", "disable", "disabled", "no", "n", "false", "0", "show"}
_STATUS = {"", "status", "check", "info"}


@Client.on_message(filters.command("privacy") & filters.private)
async def privacy_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id

    args = message.command
    arg = args[1].strip().lower() if len(args) > 1 else ""

    # An unrecognised word used to fall through to "show status", so a typo
    # like `/privacy of` looked like it had worked while changing nothing.
    if arg not in _ON and arg not in _OFF and arg not in _STATUS:
        await message.reply(
            f"❓ I don't understand `{arg[:30].replace(chr(96), chr(39))}`.\n\n"
            "**Usage:** `/privacy on` or `/privacy off` "
            "(or just `/privacy` to see your current setting)."
        )
        return

    if arg in _STATUS:
        info_doc = await db.get_user_info(user_id)
        current = bool(info_doc and info_doc.get("privacy_mode"))
        status = "🔒 **ON** — your identity is hidden" if current else "👤 **OFF** — your identity is shown normally"
        await message.reply(
            f"**Privacy mode is currently:** {status}\n\n"
            "When ON, wherever the bot would put your name, username or user "
            "ID in a file's storage caption, it shows 🔒 Anonymous instead — "
            "for the default caption and for any custom caption alike.\n\n"
            "The public download page never shows who uploaded a file.\n\n"
            "This only changes that caption text. Admins can always see your "
            "real account info through their own tools — it isn't a way to "
            "hide from moderation.\n\n"
            "**Usage:** `/privacy on` or `/privacy off`"
        )
        return

    enabled = arg in _ON
    await db.set_privacy_mode(user_id, enabled)

    if enabled:
        await message.reply(
            "🔒 **Privacy mode enabled.**\n\n"
            "From now on, your uploads show as anonymous in the storage "
            "caption instead of your real name, username and ID. This "
            "doesn't apply retroactively to files you've already uploaded — "
            "only new ones going forward.\n\n"
            "Admins can still see your real account details through their "
            "own tools; this only affects that caption text."
        )
    else:
        await message.reply(
            "👤 **Privacy mode disabled.**\n\n"
            "Your uploads will show your real name/username/ID in the "
            "storage caption again, going forward."
        )
