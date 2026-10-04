"""
Password-protected links — /lock and /unlock.

Everything this bot produces is a public URL: whoever has the link has the
file, and there is no account, no session and no way to revoke anything short
of deleting the file. That's fine while a link goes to one person, and there's
no answer at all once it's been forwarded around — which is exactly the moment
someone wants to put a door on it.

/lock stores a salted PBKDF2 digest on the file record and web/locks.py gates
every route that would otherwise hand the file (or even its *name*, which is
why /file, /info, /latest and the batch grid all refuse a locked link too).
/unlock takes it back off.

Two design points worth knowing before changing this:

  • The password is captured as its own message and deleted immediately.
    `/lock <id> <password>` in one line would store the secret permanently in
    the chat history, the bot's message log, and every backup of either.

  • Unlocking mints a signed cookie rather than re-checking the password per
    request. The KDF costs a fraction of a second to several, and a single
    video view is dozens of range requests — re-running it on the streaming
    path would turn one playback into a CPU denial-of-service on a small host.
    Because a cross-site form would have to *supply* the password to get a
    cookie out of this endpoint, there's nothing for a CSRF request to leak,
    so no token is involved: the password itself is the authenticator.
"""

import asyncio
import logging

from pyrogram import Client, filters, StopPropagation
from pyrogram.types import Message

import info as cfg
import lock_state
import sub_state
from utils import hash_password, is_admin, is_expired, md_escape

logger = logging.getLogger(__name__)

# Short enough that a typo gets caught by the confirmation echo, long enough
# that nobody sets "1234" on a link by accident.
MIN_PASSWORD = 4
MAX_PASSWORD = 256


def _page_url(file_uid: str) -> str:
    return f"{cfg.URL.rstrip('/')}/file/{file_uid}" if cfg.URL else ""


async def _owned_file(client: Client, message: Message, file_uid: str):
    """Resolve a target link and enforce the same owner-or-admin rule as
    /rename, /unlink and /unsub. Returns (meta, error_text)."""
    db = client.db  # type: ignore[attr-defined]
    meta = await db.get_file(file_uid)
    if not meta:
        return None, f"❌ No file found with ID `{md_escape(file_uid)}`."
    owner = meta.get("uploader_id")
    if owner != message.from_user.id and not is_admin(message.from_user.id):
        return None, (
            "⛔ **Not your file.** You can only change links you generated "
            "yourself — see them with /mylinks."
        )
    if is_expired(meta):
        return None, (
            f"⌛ `{md_escape(file_uid)}` has already expired, so there's "
            "nothing left to protect."
        )
    return meta, ""


# ── /lock ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("lock") & filters.private)
async def lock_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    user_id = message.from_user.id
    args = message.command

    if len(args) < 2:
        await message.reply(
            "**Usage:** `/lock <file ID>`\n\n"
            "**Example:** `/lock 4c06e7b21fb5`\n\n"
            "Puts a password on one of your own links. Afterwards *anyone* "
            "opening it — including you — has to enter it before the file, "
            "its name, or its stream/direct URLs are visible.\n\n"
            "I'll ask for the password as a separate message and delete it "
            "from this chat as soon as I've read it, so don't type it here. "
            "Find the file ID with /mylinks (it's the last part of the link).\n\n"
            "Remove it again with `/unlock <file ID>`."
        )
        return

    file_uid = args[1].strip()
    meta, error = await _owned_file(client, message, file_uid)
    if error:
        await message.reply(error)
        return

    if meta.get("lock"):
        name = md_escape(str(meta.get("file_name") or file_uid))[:80]
        await message.reply(
            f"🔒 `{name}` is already password protected.\n\n"
            "Send /unlock to remove it first — putting a new password on "
            "straight away would sign out everyone who already has the link, "
            "with no warning."
        )
        return

    if cfg.URL:
        await message.reply(f"🔗 Link: {_page_url(file_uid)}")

    # A pending /sub would otherwise swallow the next thing this user sends:
    # /sub captures documents at group=2, and if they now send a subtitle file
    # to finish /lock it would be attached to the old video. One flow at a
    # time — releasing the other one is the honest answer, and it costs the
    # user nothing but a re-typed command.
    if sub_state.is_pending(user_id):
        sub_state.clear_pending(user_id)
        await message.reply("ℹ️ Cancelled your pending /sub subtitle upload first, "
                            "so the two don't get mixed up.")
    lock_state.clear_pending(user_id)

    prompt = await message.reply(
        "🔑 **Send the password you want as a plain message** (nothing else).\n\n"
        f"Between {MIN_PASSWORD} and {MAX_PASSWORD} characters. It's stored as a "
        "salted hash — never as text — and this message and yours are both "
        "deleted as soon as I've read it.\n\n"
        "Nothing happens if you change your mind: this request expires by "
        "itself in a few minutes and the link stays open."
    )
    lock_state.set_pending(user_id, file_uid, message.chat.id, prompt.id)


@Client.on_message(filters.private & filters.text, group=2)
async def password_capture(client: Client, message: Message):
    """
    Second half of /lock. Runs in group 2 like sub_capture, so it's ahead of
    auto_help's "I work with files, not text" fallback (group 5) and behind
    the setup wizard's own text input (group -2), which claims the user's next
    message wholesale whenever a wizard is open.
    """
    user_id = message.from_user.id
    pending = lock_state.get_pending(user_id)
    if not pending:
        return  # not mid-/lock — this is an ordinary message, nobody's business

    file_uid, chat_id, prompt_id = pending
    password = (message.text or "")

    # A command typed while a /lock is open belongs to its own handler — same
    # rule the wizard and the owner-claim input learned the hard way. Without
    # it, `/cancel` or `/id` mid-flow would be hashed as a password.
    if password.startswith("/"):
        return

    password = password.strip()
    db = client.db  # type: ignore[attr-defined]

    async def _wipe_prompt() -> None:
        """Delete the outstanding /lock prompt, if it's still around."""
        if not prompt_id:
            return
        try:
            await client.delete_messages(chat_id or message.chat.id, prompt_id)
        except Exception as e:
            logger.debug("Could not delete the /lock prompt: %s", e)

    async def _forget(*extra_ids) -> None:
        """Delete the user's password message (and anything else named).

        Every exit from this handler has to do it, not just the successful one:
        the prompt promised "this message and yours are both deleted as soon as
        I've read it", and on the error paths below the message being left
        behind is the plaintext password itself — the one outcome that must not
        happen. Reply first, because Telegram refuses a reply to a deleted
        message.
        """
        ids = [m for m in (message.id, *extra_ids) if m]
        try:
            await client.delete_messages(chat_id or message.chat.id, ids)
        except Exception as e:
            logger.warning("Could not delete a password message: %s", e)

    if not (MIN_PASSWORD <= len(password) <= MAX_PASSWORD):
        # Deliberately keeps the pending entry: a mistyped one-word password is
        # the common case, and dropping the flow on it means re-typing the
        # whole command.
        await message.reply(
            f"⚠️ That's {len(password)} characters — I need between "
            f"{MIN_PASSWORD} and {MAX_PASSWORD}. Send another one, or /cancel."
        )
        await _forget()
        raise StopPropagation

    meta, error = await _owned_file(client, message, file_uid)
    if error or not meta:
        lock_state.clear_pending(user_id)
        await message.reply(error or f"❌ `{md_escape(file_uid)}` is no longer available.")
        await _forget()
        await _wipe_prompt()
        raise StopPropagation
    if meta.get("lock"):
        lock_state.clear_pending(user_id)
        await message.reply(
            "ℹ️ That link already has a password on it now, so nothing was changed."
        )
        await _forget()
        await _wipe_prompt()
        raise StopPropagation

    # 120k PBKDF2 iterations is ~1s of pure CPU on a small box, and this handler
    # shares the one event loop with every in-flight stream — so it goes to a
    # worker thread exactly like the web layer's verify path (web/locks.py).
    stored = await asyncio.to_thread(hash_password, password)

    # Reply first, then wipe: Telegram refuses a reply to a deleted message, so
    # deleting before confirming would silently swallow the "password set"
    # receipt (and the failure notice below) whenever the user needed to read
    # one. Both paths still end with the plaintext gone; only the order differs.
    if not await db.set_file_lock(file_uid, stored):
        lock_state.clear_pending(user_id)
        await message.reply(
            f"❌ Couldn't save the lock for `{md_escape(file_uid)}` — the "
            "password was thrown away and the link is unchanged. Try /lock again."
        )
        await _forget()
        await _wipe_prompt()
        raise StopPropagation

    lock_state.clear_pending(user_id)
    name = md_escape(str(meta.get("file_name") or file_uid))[:80]
    where = (f"{_page_url(file_uid)} now opens a password prompt; the file, its "
             "name and its stream/download links are hidden until it's entered."
             if cfg.URL else
             "The next link opened from this server will ask for it — no site "
             "URL is configured, so there's nothing to point at yet.")
    await message.reply(
        f"🔒 **Password set on `{name}`.**\n\n"
        f"{where}\n\n"
        f"`/unlock {md_escape(file_uid)}` removes the password again."
    )
    await _forget()
    await _wipe_prompt()
    raise StopPropagation


# ── /unlock ──────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("unlock") & filters.private)
async def unlock_command(client: Client, message: Message):
    db = client.db  # type: ignore[attr-defined]
    args = message.command

    if len(args) < 2:
        await message.reply(
            "**Usage:** `/unlock <file ID>`\n\n"
            "**Example:** `/unlock 4c06e7b21fb5`\n\n"
            "Removes the password from one of your own links, so it opens "
            "for anyone who has the URL again. Find the ID with /mylinks."
        )
        return

    file_uid = args[1].strip()
    meta, error = await _owned_file(client, message, file_uid)
    if error:
        await message.reply(error)
        return
    if not meta.get("lock"):
        await message.reply(
            f"ℹ️ `{md_escape(file_uid)}` isn't password protected — nothing to remove."
        )
        return

    if not await db.set_file_lock(file_uid, None):
        await message.reply(f"❌ Couldn't update `{md_escape(file_uid)}` — try again.")
        return

    name = md_escape(str(meta.get("file_name") or file_uid))[:80]
    where = (f"{_page_url(file_uid)} is open to anyone with the link again."
             if cfg.URL else "Links from this server are open again to anyone who has one.")
    await message.reply(
        f"🔓 **Password removed from `{name}`.**\n"
        f"{where}\n\n"
        "Anyone still holding an unlock cookie from the previous password "
        "stops being able to use it as soon as a new one is set."
    )
