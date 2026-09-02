"""
/timezone — lets any user set their own timezone so dates/times shown to
them (in /status, /userinfo when viewed by an admin looking at themselves,
ban-expiry notices, etc.) display in their local time instead of always UTC.

Accepts real IANA names ("Asia/Kolkata"), common abbreviations ("IST",
"EST", "GMT"), or a plain city/region name ("kolkata", "new york") — see
utils.resolve_timezone for the exact resolution order.
"""

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from utils import resolve_timezone, format_local_time, timezone_offset_str, POPULAR_TIMEZONES
from datetime import datetime


def _quick_pick_markup() -> InlineKeyboardMarkup:
    """A short list of well-known timezones as tappable buttons — typing an
    exact IANA name is unfriendly, and this covers the common case in one
    tap while still allowing anyone to type something else."""
    rows = []
    row = []
    for tz in POPULAR_TIMEZONES:
        label = tz.split("/")[-1].replace("_", " ")
        row.append(InlineKeyboardButton(label, callback_data=f"tzset_{tz}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _status_text(tz_name: "str | None") -> str:
    now = datetime.utcnow()
    if tz_name:
        offset = timezone_offset_str(tz_name)
        current = format_local_time(now, tz_name)
        return (
            f"🕐 **Your timezone:** `{tz_name}` ({offset})\n"
            f"**Current time there:** {current}"
        )
    return (
        "🕐 **Your timezone:** not set — showing **UTC** by default.\n"
        f"**Current UTC time:** {format_local_time(now, None)}"
    )


@Client.on_message(filters.command("timezone") & filters.private)
async def timezone_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id

    args = message.command
    if len(args) < 2:
        info_doc = await db.get_user_info(user_id)
        current_tz = info_doc.get("timezone") if info_doc else None
        await message.reply(
            f"{_status_text(current_tz)}\n\n"
            "**To change it:** `/timezone <name>` — a real timezone name "
            "(`Asia/Kolkata`), a common abbreviation (`IST`, `EST`, "
            "`GMT`), or just a city (`kolkata`, `london`).\n\n"
            "Or tap one below:",
            reply_markup=_quick_pick_markup(),
        )
        return

    query = message.text.split(None, 1)[1].strip()

    if query.lower() in ("off", "reset", "clear", "default"):
        await db.set_timezone(user_id, None)
        await message.reply(
            "✅ **Timezone reset.** Times will show in **UTC** again."
        )
        return

    resolved, suggestions = resolve_timezone(query)

    if resolved:
        await db.set_timezone(user_id, resolved)
        offset = timezone_offset_str(resolved)
        current = format_local_time(datetime.utcnow(), resolved)
        await message.reply(
            f"✅ **Timezone set to** `{resolved}` ({offset})\n"
            f"**Current time there:** {current}\n\n"
            "Send `/timezone off` any time to go back to UTC."
        )
        return

    if suggestions:
        buttons = [
            [InlineKeyboardButton(s, callback_data=f"tzset_{s}")]
            for s in suggestions[:8]
        ]
        await message.reply(
            f"🤔 `{query}` matches more than one timezone — pick one:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    await message.reply(
        f"❌ Couldn't find a timezone matching `{query}`.\n\n"
        "Try a real name (`Asia/Kolkata`), a common abbreviation "
        "(`IST`, `EST`, `GMT`), or just a city name (`kolkata`).",
        reply_markup=_quick_pick_markup(),
    )


@Client.on_callback_query(filters.regex(r"^tzset_(.+)$"))
async def timezone_quickpick_cb(client: Client, cq: CallbackQuery):
    db = client.db  # type: ignore[attr-defined]
    tz_name = cq.data.split("_", 1)[1]

    resolved, _ = resolve_timezone(tz_name)
    if not resolved:
        await cq.answer("That timezone isn't valid — please try again.", show_alert=True)
        return

    await db.set_timezone(cq.from_user.id, resolved)
    offset = timezone_offset_str(resolved)
    current = format_local_time(datetime.utcnow(), resolved)
    await cq.answer(f"Timezone set to {resolved}")
    await cq.message.edit_text(
        f"✅ **Timezone set to** `{resolved}` ({offset})\n"
        f"**Current time there:** {current}\n\n"
        "Send `/timezone off` any time to go back to UTC."
    )
