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

import difflib
import logging
import re

from pyrogram import Client, filters
from pyrogram.types import BotCommandScopeDefault
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import batch_state
from info import BOT_NAME
from utils import md_escape, is_admin

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
            f"Hey {md_escape(user.first_name or 'there')} 👋\n\n"
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
            f"Hey {md_escape(user.first_name or 'there')}! Short version: send me any file and "
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


# ── Unknown / mistyped commands ──────────────────────────────────────────────
# The text handler above deliberately ignores anything starting with "/", so a
# typo like "/hepl" or "/btach" used to get total silence — indistinguishable
# from the bot being down. This answers those, with a "did you mean…?" pulled
# from the bot's own public command menu so it can never drift out of date.
#
# Whether a command "exists" is decided by walking the handlers that are
# actually registered (not a hand-kept list), so adding a new command never
# needs a change here — and a real command is never called unknown.

_COMMAND_TOKEN = re.compile(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?")
_PUBLIC_MENU_CACHE: list = []


def _collect_commands(flt, out: set) -> None:
    """Gather command names from a (possibly composed) Pyrogram filter."""
    if flt is None:
        return
    names = getattr(flt, "commands", None)
    if isinstance(names, (set, list, tuple, frozenset)):
        out.update(str(n).lower() for n in names)
    for attr in ("base", "other"):      # And / Or / Invert filters nest here
        _collect_commands(getattr(flt, attr, None), out)


def _registered_commands(client: Client) -> set:
    known = getattr(client, "_known_commands", None)
    if known:
        return known
    known = set()
    for handlers in client.dispatcher.groups.values():
        for handler in handlers:
            _collect_commands(getattr(handler, "filters", None), known)
    if known:                           # don't cache an empty (too-early) scan
        client._known_commands = known  # type: ignore[attr-defined]
    return known


async def _public_menu(client: Client) -> list:
    if not _PUBLIC_MENU_CACHE:
        try:
            cmds = await client.get_bot_commands(scope=BotCommandScopeDefault())
            _PUBLIC_MENU_CACHE.extend(c.command for c in cmds)
        except Exception:
            pass
    return _PUBLIC_MENU_CACHE


# Commands whose handlers use a silent admin filter (plugins/admin.py).
_SILENT_ADMIN_COMMANDS = frozenset({
    "stats", "users", "userinfo", "ban", "unban", "unwarn", "broadcast",
    "recentfiles", "addadmin", "admins", "deleteall", "confirmdeleteall",
    "serverstatus",
})


@Client.on_message(filters.private & filters.text & filters.regex(r"^/"), group=9)
async def unknown_command(client: Client, message: Message):
    m = _COMMAND_TOKEN.match(message.text or "")
    if not m:
        return
    name, target = m.group(1).lower(), m.group(2)
    # "/start@SomeOtherBot" is meant for a different bot; stay out of it.
    me = getattr(client, "me", None)
    if target and me and target.lower() != (me.username or "").lower():
        return
    # Admin commands guarded by a silent filter gave non-admins no answer at
    # all, which looks like the bot is down. Say what's going on instead.
    if name in _SILENT_ADMIN_COMMANDS and not (
        message.from_user and is_admin(message.from_user.id)
    ):
        await message.reply("⛔ That command is for admins only.")
        return
    known = _registered_commands(client)
    if not known or name in known:
        return      # a real command (handled elsewhere), or we can't tell yet

    guesses = difflib.get_close_matches(name, await _public_menu(client), n=2, cutoff=0.6)
    text = f"❓ I don't know the command `/{name}`."
    if guesses:
        text += "\n\nDid you mean " + " or ".join(f"/{g}" for g in guesses) + "?"
    text += "\n\nSend /help to see everything I can do."
    await message.reply(text, quote=True)
