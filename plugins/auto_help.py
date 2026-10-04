"""
Auto Guide & Help — proactive, command-free onboarding.
========================================================
Every UX path in this bot so far requires the user to already know a
command exists (/start, /help, /about) or to notice a button. Someone who
just opens a chat with the bot and types "hi", pastes a link, or sends an
unsupported message type (a contact, a location, a poll…) currently gets
nothing back — total silence, no guidance, no /help required or offered.

This module closes that gap by reacting to what people actually do,
without ever requiring a command:

  - A brand-new user's very FIRST message — whatever it is, file or
    text — gets the "how this works" walkthrough automatically. They
    never have to discover /start.
  - Plain text that isn't a command gets a situation-aware reply instead
    of silence: a greeting gets a warm hello + quick guide, a pasted URL
    gets steered toward "send the actual file", a mid-batch user typing
    instead of sending files gets reminded of /done and /cancel, and
    anything else gets a short "here's what I can do" nudge.
  - Message kinds we don't otherwise handle (contact, location, poll,
    venue, dice, game) get a friendly "here's what I *can* accept" reply
    instead of vanishing into the void.

Runs in group=5 — after antispam (-1), file handling (0), and batch file
collection (1) — so it only ever fires for messages nothing else claimed.
Real commands (anything starting with "/") are explicitly skipped up
front: this module only ever fills silence, never talks over another
handler's reply.
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import batch_state
from info import BOT_NAME

logger = logging.getLogger(__name__)

_GREETING_WORDS = {
    "hi", "hii", "hiii", "hello", "hey", "heya", "yo", "sup",
    "hola", "salut", "namaste", "morning", "evening",
}
_HELP_WORDS = {
    "help", "how", "guide", "instructions", "usage", "support",
    "stuck", "confused", "what", "?",
}


def _quick_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 How to Use", callback_data="help")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
    ])


def _looks_like_url(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith(("http://", "https://", "www.")) or "t.me/" in t


async def _is_mid_batch(client: Client, user_id: int) -> bool:
    if batch_state.is_in_batch(user_id):
        return True
    try:
        db = client.db  # type: ignore[attr-defined]
        return bool(await db.get_user_active_batch(user_id))
    except Exception:
        return False


@Client.on_message(filters.private & filters.text, group=5)
async def auto_guide_text(client: Client, message: Message):
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return  # a real command — some other handler already answered it

    user = message.from_user
    if not user:
        return
    db = client.db  # type: ignore[attr-defined]

    try:
        is_new = await db.add_user(user.id, user.first_name or "", user.username or "")
        await db.mark_active(user.id)
    except Exception as e:
        logger.debug("Could not record user %s in auto-guide: %s", user.id, e)
        is_new = False

    # Mid-batch users who typed instead of sending a file or /done — most
    # useful check to run first, since it overrides every other guess.
    if await _is_mid_batch(client, user.id):
        await message.reply(
            "You're mid-batch right now — keep sending files to add them, "
            "or type /done when you're ready for the link, /cancel if you "
            "want to scrap it.",
        )
        return

    if is_new:
        await message.reply(
            f"Hey {user.first_name} 👋\n\n"
            f"I'm {BOT_NAME}. Send me any file — no typing needed — and "
            "I'll hand you back a download link, a stream link if it's "
            "video or audio, and a web page for it too, all automatically.\n\n"
            "Got more than one to share? Send them one after another and "
            "I'll offer to bundle them into a single batch link.",
            reply_markup=_quick_buttons(),
        )
        return

    if _looks_like_url(text):
        await message.reply(
            "That looks like a link, not a file — I can only work with "
            "what you send me directly, I can't go fetch something from "
            "another site. Send over the actual document, video, audio, "
            "or photo and I'll take it from there.",
            reply_markup=_quick_buttons(),
        )
        return

    lowered = text.lower().strip("!.,? ")
    words = set(lowered.split())
    if (words & _GREETING_WORDS) or (words & _HELP_WORDS) or "?" in text:
        await message.reply(
            f"Hey {user.first_name}! Short version: send me any file and "
            "I'll turn it into a link — a download link, and a stream "
            "link too if it's video or audio.\n\n"
            "Send a few in a row and I'll offer to bundle them into one "
            "batch link instead. That's really all there is to it.",
            reply_markup=_quick_buttons(),
        )
        return

    # Fallback for anything else typed.
    await message.reply(
        "I only really do files, not text — send me a document, video, "
        "audio, or photo and I'll turn it into a link right away.",
        reply_markup=_quick_buttons(),
    )


@Client.on_message(
    filters.private & (
        filters.contact | filters.location | filters.venue |
        filters.poll | filters.dice | filters.game
    ),
    group=5,
)
async def auto_guide_unsupported(client: Client, message: Message):
    """Message kinds nothing else handles — friendly nudge instead of silence."""
    await message.reply(
        "That's not something I can turn into a link, unfortunately — I "
        "work with documents, videos, audio, and photos. Send one of "
        "those over and I'll take it from there.",
        reply_markup=_quick_buttons(),
    )
