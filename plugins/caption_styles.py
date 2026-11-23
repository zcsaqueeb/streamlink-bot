"""
/captions - pick how new uploads are captioned, from ready-made styles.

Writing a /setcaption template by hand works but is fiddly; this shows every
style with a live preview and a button to switch to it. It also holds the
on/off switch for the uploader block in the built-in caption, so the whole
"what does the caption say about the uploader?" decision lives in one place.

Reachable three ways: /captions, the "Caption Style" button in /settings, and
`/setcaption <style name>` (see plugins/caption.py).

Admin only. Changes apply to the very next upload; existing files keep the
caption they were stored with.
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import settings_store
from caption_presets import (
    CAPTION_PRESETS, PRESETS_BY_KEY, SampleMessage, current_preset, render_sample,
    shows_identity,
)
from utils import build_uploader_caption, is_admin

logger = logging.getLogger(__name__)

_NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
_TG_TEXT_LIMIT = 4000   # Telegram allows 4096; stay clear of it


async def _preview(preset: dict) -> str:
    if preset["key"] == "default":
        # The real built-in caption, so the preview honours the on/off switch.
        return await build_uploader_caption(
            SampleMessage(), "example_video.mp4", 254_800_000, db=None, template=""
        )
    return render_sample(preset["template"])


async def build_panel() -> tuple[str, InlineKeyboardMarkup]:
    """Text and buttons for the style picker, reflecting the saved settings."""
    template = settings_store.get("custom_caption_template", "") or ""
    current = current_preset(template)
    show_info = bool(settings_store.get("show_uploader_info", False))

    if current is not None:
        now = f"**{current['label']}**"
    else:
        now = "**Custom template** (written with /setcaption)"

    blocks = []
    for i, p in enumerate(CAPTION_PRESETS):
        mark = "✅ " if current is p else ""
        head = f"{_NUMBER_EMOJI[i]} {mark}**{p['label']}** — {p['desc']}"
        blocks.append((head, await _preview(p)))

    def _compose(with_previews: bool) -> str:
        lines = [
            "🎨 **Caption style**",
            "",
            "How each new upload is captioned in the storage channel.",
            f"**Current:** {now}",
            "",
        ]
        for head, preview in blocks:
            lines.append(head)
            if with_previews:
                lines.append(preview)
            lines.append("")
        lines += [
            "🔒 Users who turned on /privacy always show as Anonymous in "
            "uploader fields, whichever style you pick.",
            "✍️ Want your own? Use /setcaption, or `/setcaption card` to pick "
            "a style by name.",
        ]
        return "\n".join(lines)

    text = _compose(True)
    if len(text) > _TG_TEXT_LIMIT:      # many long previews: fall back to names only
        text = _compose(False)

    buttons = [
        InlineKeyboardButton(
            f"{'✅ ' if current is p else ''}{i + 1}. {p['label']}",
            callback_data=f"capset:{p['key']}",
        )
        for i, p in enumerate(CAPTION_PRESETS)
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(
        f"👤 Uploader info in built-in caption: {'ON' if show_info else 'OFF'}",
        callback_data="capshow:toggle",
    )])
    rows.append([InlineKeyboardButton("⬅️ Back to Settings", callback_data="settingsmenu")])
    return text, InlineKeyboardMarkup(rows)


async def _refresh(cq: CallbackQuery) -> None:
    text, markup = await build_panel()
    try:
        await cq.message.edit_text(text, reply_markup=markup)
    except Exception as e:
        # "message not modified" when nothing changed is expected and harmless.
        logger.debug("caption panel refresh skipped: %s", e)


@Client.on_message(filters.command("captions") & filters.private)
async def captions_command(client: Client, message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.reply("⛔ Admins only.")
        return
    text, markup = await build_panel()
    await message.reply(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^capmenu$"))
async def caption_menu_cb(client: Client, cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ Admins only.", show_alert=True)
        return
    await cq.answer()
    text, markup = await build_panel()
    await cq.message.reply(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^capset:"))
async def caption_set_cb(client: Client, cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ Admins only.", show_alert=True)
        return
    preset = PRESETS_BY_KEY.get(cq.data.split(":", 1)[1])
    if preset is None:
        await cq.answer("That style no longer exists.", show_alert=True)
        return
    settings_store.set("custom_caption_template", preset["template"])
    note = " (shows who uploaded)" if shows_identity(preset) else ""
    await cq.answer(f"✅ {preset['label']}{note}")
    await _refresh(cq)


@Client.on_callback_query(filters.regex(r"^capshow:toggle$"))
async def caption_show_toggle_cb(client: Client, cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ Admins only.", show_alert=True)
        return
    new_state = not bool(settings_store.get("show_uploader_info", False))
    settings_store.set("show_uploader_info", new_state)
    await cq.answer(f"Uploader info {'ON' if new_state else 'OFF'}")
    await _refresh(cq)
