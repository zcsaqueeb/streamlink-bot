"""
Admin-configurable caption template.

Lets an admin save a custom caption (with placeholders) via a Telegram
command; every file uploaded afterward has its stored caption rewritten
using that template instead of the built-in default. See
utils.render_caption_template for the exact placeholder syntax.
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import Message

import settings_store
from utils import is_admin, render_caption_template

logger = logging.getLogger(__name__)

_PLACEHOLDER_HELP = (
    "**Placeholders you can use:**\n"
    "▸ `{filename}` — the file's name\n"
    "▸ `{size}` — file size (e.g. `24.3 MB`)\n"
    "▸ `{uploader}` — uploader's display name\n"
    "▸ `{username}` — uploader's @username\n"
    "▸ `{user_id}` — uploader's numeric Telegram ID\n"
    "▸ `{date}` — upload date & time (UTC)"
)


@Client.on_message(filters.command("setcaption") & filters.private)
async def set_caption_cmd(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        await message.reply("⛔ Admins only.")
        return

    # Accept the template either as command args OR as the text of a
    # replied-to message — replying is much easier for a multi-line
    # template than typing it all inline after /setcaption.
    template = None
    if message.reply_to_message and message.reply_to_message.text:
        template = message.reply_to_message.text
    elif len(message.command) > 1:
        template = message.text.split(None, 1)[1]

    if not template:
        await message.reply(
            "**Usage:**\n"
            "`/setcaption <your template>`\n\n"
            "…or reply to any message containing your template with "
            "`/setcaption` (easiest for multi-line templates).\n\n"
            f"{_PLACEHOLDER_HELP}\n\n"
            "**Example:**\n"
            "`/setcaption 📁 {filename}\\n💾 {size}\\n🙋 Shared by {uploader}`"
        )
        return

    # Validate: render it once against dummy values so an admin gets
    # immediate feedback on what it will actually look like, and so a
    # template that raises for some unexpected reason is caught here
    # rather than silently falling back to the default on every future
    # upload with no explanation why.
    try:
        preview = _render_preview(template)
    except Exception as e:
        await message.reply(
            f"⚠️ Couldn't render that template: `{e}`\n"
            "It was **not** saved — please check the placeholders and try again."
        )
        return

    settings_store.set("custom_caption_template", template)

    await message.reply(
        "✅ **Caption template saved.**\n"
        "Every file uploaded from now on will use this caption.\n\n"
        "**Preview with sample values:**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{preview}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Use `/getcaption` to view it again, or `/resetcaption` to go back "
        "to the default."
    )


@Client.on_message(filters.command("getcaption") & filters.private)
async def get_caption_cmd(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        await message.reply("⛔ Admins only.")
        return

    template = (settings_store.get("custom_caption_template", "") or "").strip()
    if not template:
        await message.reply(
            "ℹ️ No custom caption template is set — new uploads use the "
            "built-in default caption.\n\n"
            f"{_PLACEHOLDER_HELP}\n\n"
            "Set one with `/setcaption <template>`."
        )
        return

    try:
        preview = _render_preview(template)
    except Exception:
        preview = "_(preview unavailable)_"

    await message.reply(
        "**Current caption template:**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"`{template}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "**Preview with sample values:**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{preview}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


@Client.on_message(filters.command("resetcaption") & filters.private)
async def reset_caption_cmd(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        await message.reply("⛔ Admins only.")
        return

    had_template = bool((settings_store.get("custom_caption_template", "") or "").strip())
    settings_store.set("custom_caption_template", "")

    if had_template:
        await message.reply(
            "✅ **Caption template cleared.** New uploads will use the "
            "built-in default caption again."
        )
    else:
        await message.reply("ℹ️ No custom caption template was set — nothing to reset.")


def _render_preview(template: str) -> str:
    """Render `template` against a fake Message-like object with sample
    values, purely for admin preview purposes — never used for real
    uploads (real uploads always call utils.build_uploader_caption with
    the actual Message)."""

    class _FakeUser:
        first_name = "Jordan"
        last_name = "Lee"
        username = "jordanlee"
        id = 123456789

    class _FakeMessage:
        from_user = _FakeUser()

    return render_caption_template(template, _FakeMessage(), "example_video.mp4", 254_800_000)
