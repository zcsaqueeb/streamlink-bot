"""
/start, /help, /about — upgraded with more buttons, smooth UX, and link expiry support.
"""

import logging
from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from info import BOT_NAME, DB_CHANNEL, CREATOR_NAME
from utils import check_force_sub, humanbytes, is_admin, is_expired, md_escape
from plugins.setup import maybe_claim_owner, _send_settings_menu

logger = logging.getLogger(__name__)


# ── Button layouts ────────────────────────────────────────────────────────────

def _main_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 How to Use", callback_data="help"),
            InlineKeyboardButton("ℹ️ About",       callback_data="about"),
        ],
        [
            InlineKeyboardButton("🌐 Web Portal",  callback_data="web"),
            InlineKeyboardButton("⚙️ Settings",    callback_data="settings"),
        ],
        [
            InlineKeyboardButton("👨‍💻 Support",     callback_data="support"),
            InlineKeyboardButton("📊 My Stats",    callback_data="mystats"),
        ],
    ])


async def _edit(cq, text, **kwargs):
    """edit_text that tolerates "message is not modified" (a button tapped
    twice, or Back onto an identical screen) instead of raising."""
    try:
        await cq.message.edit_text(text, **kwargs)
    except MessageNotModified:
        pass


def _back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="home")]])


# ── /start ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    user = message.from_user
    db = client.db  # type: ignore[attr-defined]

    if await maybe_claim_owner(client, message):
        return

    try:
        await db.mark_active(user.id)
        await db.add_user(user.id, user.first_name or "", user.username or "")
    except Exception as e:
        logger.warning("Could not save user %s: %s", user.id, e)

    # ── Deep-link: /start <file_uid> ─────────────────────────────────────────
    args = message.command
    if len(args) > 1:
        file_uid = args[1]

        if not await check_force_sub(client, message):
            return

        try:
            file_meta = await db.get_file(file_uid)
        except Exception:
            file_meta = None

        if not file_meta:
            await message.reply(
                "❌ **File not found.**\n"
                "It may have been deleted. Contact the uploader for a new link.",
                reply_markup=_back_button(),
            )
            return

        # Check expiry — the shared predicate in utils.py, because the web
        # routes and /sub have to give the same answer as this reply.
        if is_expired(file_meta):
            await message.reply(
                "⏰ **Link Expired**\n\n"
                "This file link has expired and is no longer valid.\n"
                "Please ask the uploader to share a new link.",
                reply_markup=_back_button(),
            )
            return

        # A password-protected link must not be handed out here: this path
        # copies the file itself, bypassing the web password prompt. The
        # uploader and admins may still fetch their own file.
        if file_meta.get("lock") and file_meta.get("uploader_id") != user.id \
                and not is_admin(user.id):
            from info import URL as _URL
            await message.reply(
                "🔒 **This file is password protected.**\n"
                + (f"Open {_URL.rstrip('/')}/file/{file_uid} and enter the "
                   "password to get it." if _URL else
                   "Ask the uploader for the password and the web link."),
                reply_markup=_back_button(),
                disable_web_page_preview=True,
            )
            return

        try:
            msg = await client.get_messages(DB_CHANNEL, int(file_meta["msg_id"]))
            if getattr(msg, "empty", False):
                raise ValueError("storage message no longer exists")

            # BUG FIX #1 — build caption from stored metadata only (never
            # from msg.caption, which already contains the filename written
            # by build_uploader_caption). This guarantees the filename
            # appears exactly once in what the user receives.
            file_name = file_meta.get("file_name") or "File"
            safe_name = md_escape(str(file_name).replace("`", "'"))
            safe_mime = str(file_meta.get("mime_type") or "unknown").replace("`", "'")
            caption = (
                f"📁 **{safe_name}**\n"
                f"💾 **Size:** {humanbytes(file_meta.get('file_size', 0))}\n"
                f"📂 **Type:** `{safe_mime}`"
            )

            # Build web links if URL is configured
            from info import URL
            buttons = []
            if URL:
                row = [InlineKeyboardButton("⬇️ Download", url=f"{URL}/download/{file_uid}")]
                if file_meta.get("type") in ("video", "audio", "voice", "video_note", "animation"):
                    row.append(InlineKeyboardButton("▶️ Stream", url=f"{URL}/stream/{file_uid}"))
                buttons.append(row)
                buttons.append([InlineKeyboardButton("🌐 Open Web Page", url=f"{URL}/file/{file_uid}")])

            markup = InlineKeyboardMarkup(buttons) if buttons else None
            # Pass caption= explicitly so Pyrogram uses our clean one-time
            # display caption instead of the raw storage-channel caption.
            await msg.copy(message.chat.id, caption=caption, reply_markup=markup)
        except Exception as e:
            logger.error("File delivery error for %s: %s", file_uid, e)
            await message.reply(
                "❌ **Could not retrieve the file.**\n"
                "The file may have been removed from storage, or the storage channel is misconfigured. Try again later or contact the bot admin."
            )
        return

    # ── Normal /start ─────────────────────────────────────────────────────────
    await message.reply(
        f"Hey {md_escape(user.first_name or 'there')} 👋\n\n"
        f"I'm **{BOT_NAME}** — send me any file and get back an instant "
        "download & stream link. No sign-up, no waiting.\n\n"
        "Multiple files? Use /batch to bundle them into one link.",
        reply_markup=_main_buttons(),
    )


# ── Callbacks ─────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex("^help$"))
async def help_callback(client, cq):
    await cq.answer()
    await _edit(
        cq,
        "It's simple: send me a file and I'll hand you back a link — a "
        "download link, and a stream link too if it's video or audio. "
        "Share that with whoever needs it, done.\n\n"
        "A few commands worth knowing:\n"
        "/batch — bundle several files into one link instead of sending "
        "them separately\n"
        "/mybatch — see what's in your batch so far\n"
        "/mylinks — find a link you made earlier (`/mylinks 2` for older ones)\n"
        "/search — find one of your links by file name\n"
        "/mystats — see how much your files are being used\n"
        "/sub — attach a subtitle file to a video you already uploaded\n"
        "/rename — fix a filename without changing its link (you can paste the whole link)\n"
        "/unlink — take one of your own links down\n"
        "/expire — make a link stop working after a set time\n"
        "/lock — put a password on a link you've already shared\n"
        "/ping — check how fast I'm running\n"
        "/id — grab your Telegram user ID\n\n"
        "I take documents, videos, audio, photos, voice notes, and GIFs — "
        "send whatever you've got.",
        reply_markup=_back_button(),
    )


@Client.on_callback_query(filters.regex("^about$"))
async def about_callback(client, cq):
    import sys
    await cq.answer()
    await _edit(
        cq,
        f"I'm **{BOT_NAME}**, running on Python {sys.version.split()[0]} and "
        "Pyrogram 2.0.106, with aiohttp and Jinja2 powering the web side.\n\n"
        "Every file you send lives in a private Telegram channel — no "
        "third-party storage, nothing external. Links can stream, support "
        "seeking around in a video, and expire on a schedule if the admin's "
        "set one up.\n\n"
        f"Built by {CREATOR_NAME}.",
        reply_markup=_back_button(),
    )


@Client.on_callback_query(filters.regex("^web$"))
async def web_callback(client, cq):
    from info import URL
    await cq.answer()
    if URL:
        await _edit(
        cq,
            "Every link I generate also has a web page version — "
            f"`{URL}` — where you can stream video right in the browser, "
            "download with a progress bar, and it all just works whether "
            "you're on your phone or a desktop.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Open Portal", url=URL)],
                [InlineKeyboardButton("🔙 Back", callback_data="home")],
            ]),
        )
    else:
        await _edit(
        cq,
            "The web portal isn't switched on yet — the admin can turn it "
            "on by setting a Website URL in /settings.",
            reply_markup=_back_button(),
        )


@Client.on_callback_query(filters.regex("^settings$"))
async def settings_callback(client, cq):
    # BUG FIX: this button used to show the same "ask the admin" blurb to
    # everybody — including the admin, who tapped ⚙️ Settings on their own
    # /start menu and were told to go talk to… themselves. The real settings
    # panel only existed behind the /settings command and the wizard's own
    # "Back to Settings" button (callback "settingsmenu"). Admins now land on
    # that panel directly; regular users keep the explanatory text.
    if is_admin(cq.from_user.id):
        await cq.answer()
        await _send_settings_menu(client, cq.message.chat.id, edit_message=cq.message)
        return
    await cq.answer()
    await _edit(
        cq,
        "Links keep working until they expire — by default that's never, "
        "unless the admin set a lifetime. Use /expire to make one stop "
        "working sooner (e.g. `/expire <link> 7d`), "
        "/lock to put a password on it, and /privacy to hide your name in "
        "captions. Want to send several files as one link? /batch gets "
        "that going. Force-sub, if it's enabled, is handled by the admin.\n\n"
        "Anything else you'd like changed is a conversation with the bot admin.",
        reply_markup=_back_button(),
    )


@Client.on_callback_query(filters.regex("^mystats$"))
async def mystats_callback(client, cq):
    await cq.answer()
    from plugins.myfiles import build_mystats
    db = client.db  # type: ignore[attr-defined]
    await _edit(
        cq,
        await build_mystats(db, cq.from_user.id),
        reply_markup=_back_button(),
        disable_web_page_preview=True,
    )


@Client.on_callback_query(filters.regex("^support$"))
async def support_callback(client, cq):
    await cq.answer()
    await _edit(
        cq,
        "If something's not working, a few usual suspects: Telegram caps "
        "file size at 2 GB, so anything bigger won't go through. If a link "
        "gives you a 404, the file's probably been deleted. If it says "
        "expired, just ask whoever sent it for a fresh one.\n\n"
        "Still stuck? The bot admin can help from here.",
        reply_markup=_back_button(),
    )


@Client.on_callback_query(filters.regex("^home$"))
async def home_callback(client, cq):
    await cq.answer()
    user = cq.from_user
    await _edit(
        cq,
        f"Hey {md_escape(user.first_name or 'there')} 👋\n\n"
        f"I'm **{BOT_NAME}** — send me any file and get back an instant "
        "download & stream link. No sign-up, no waiting.\n\n"
        "Multiple files? Use /batch to bundle them into one link.",
        reply_markup=_main_buttons(),
    )


# ── Text commands ─────────────────────────────────────────────────────────────

@Client.on_message(filters.command("help") & filters.private)
async def help_command(client: Client, message: Message):
    await message.reply(
        "It's simple: send me a file and I'll hand you back a link — a "
        "download link, and a stream link too if it's video or audio.\n\n"
        "A few commands that might help:\n"
        "/batch — bundle several files into one link\n"
        "/mybatch — check what's in your current batch\n"
        "/done — finish the current batch\n"
        "/cancel — cancel the current batch\n"
        "/link — get your latest link and its buttons again (`/link <ID>` for another)\n"
        "/mylinks — list your links, 10 per page (`/mylinks 2` for older)\n"
        "/search — find one of your links by file name\n"
        "/mystats — your uploads and how much they're used\n"
        "/rename — fix a file's name without breaking its link\n"
        "/unlink — take one of your own links down\n"
        "/expire — make a link stop working after a set time\n"
        "/sub — add a subtitle file to a video you uploaded\n"
        "/unsub — remove a subtitle you added with /sub\n"
        "/lock — put a password on one of your links\n"
        "/unlock — take that password back off\n"
        "/privacy — hide your identity from upload captions\n"
        "/mydata — see what I've stored about your account\n"
        "/timezone — show times in your own timezone\n"
        "/status — check your account status\n"
        "/ping — see how fast I'm running\n"
        "/id — grab your Telegram user ID\n\n"
        "If you're an admin, /status shows the bot-wide dashboard instead.\n\n"
        "**Admin commands**\n"
        "📊 /stats · /analytics · /serverstatus · /users · /userinfo · /recentfiles\n"
        "⚙️ /settings · /setup · /restart · /maintenance\n"
        "📣 /broadcast · /ban · /unban · /unwarn\n"
        "🗑 /delete `<file_id>` · /deleteall\n"
        "👥 /addadmin · /admins\n"
        "🎨 /captions — pick a caption style (incl. how uploader info shows)\n"
        "✍️ /setcaption, /getcaption, /resetcaption — rewrite the caption "
        "used on newly uploaded files.\n\n"
        "I can handle documents, videos, audio, photos, voice notes, GIFs, "
        "and stickers.",
        reply_markup=_main_buttons(),
    )


@Client.on_message(filters.command("about") & filters.private)
async def about_command(client: Client, message: Message):
    import sys
    await message.reply(
        f"I'm **{BOT_NAME}**, running on Python {sys.version.split()[0]} and "
        "Pyrogram 2.0.106.\n\n"
        "I turn any file you send into a permanent link, and everything's "
        "stored in a private channel — no external servers involved.\n\n"
        f"Built by {CREATOR_NAME}.",
    )
