"""
Extra stats/ping commands available to all users, plus /analytics
(admin-only) for a visual chart of real upload/user-growth trends.
"""

import asyncio
import io
import time
from collections import defaultdict
from datetime import datetime, timedelta

from pyrogram import Client, filters
from pyrogram.types import Message

from utils import is_admin

admin_filter = filters.create(lambda _, __, m: m.from_user and is_admin(m.from_user.id))

# matplotlib is optional — /analytics degrades to a clear message instead of
# breaking anything else if it (or its Agg backend) isn't installed. This is
# the same optional-dependency pattern used for python-magic elsewhere in
# this codebase (see web/cache.py).
try:
    import matplotlib
    matplotlib.use("Agg")  # headless, no display server needed
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except Exception:
    MATPLOTLIB_AVAILABLE = False


@Client.on_message(filters.command("ping") & filters.private)
async def ping_command(client: Client, message: Message):
    start = time.monotonic()
    msg = await message.reply("🏓 Pong!")
    elapsed = (time.monotonic() - start) * 1000
    await msg.edit(f"🏓 **Pong!**\n⚡ `{elapsed:.2f} ms`")


@Client.on_message(filters.command("id") & filters.private)
async def id_command(client: Client, message: Message):
    """Return the user's Telegram ID (and target user if replying)."""
    lines = [f"🆔 **Your ID:** `{message.from_user.id}`"]
    if message.reply_to_message and message.reply_to_message.from_user:
        ru = message.reply_to_message.from_user
        lines.append(f"👤 **Target ID:** `{ru.id}` ({ru.first_name})")
    if message.reply_to_message and message.reply_to_message.forward_from:
        fu = message.reply_to_message.forward_from
        lines.append(f"🔁 **Forwarded-from ID:** `{fu.id}` ({fu.first_name})")
    await message.reply("\n".join(lines))


# ── /analytics — visual trend chart, backed by real DB data ─────────────────
_MIN_DAYS = 2
_MAX_DAYS = 90
_DEFAULT_DAYS = 14


def _parse_days_arg(message: Message) -> "int | str":
    """Returns a valid day count, or an error string to show the user."""
    args = message.command
    if len(args) < 2:
        return _DEFAULT_DAYS
    raw = args[1].strip()
    if not raw.isdigit():
        return f"`{raw}` isn't a number. Send a day count, e.g. `/analytics 30`."
    days = int(raw)
    if days < _MIN_DAYS or days > _MAX_DAYS:
        return f"Please pick a day count between {_MIN_DAYS} and {_MAX_DAYS}."
    return days


def _bucket_by_day(timestamps: list, days: int) -> list:
    """Turn a list of datetimes into a fixed-length list of per-day counts,
    oldest day first, covering exactly `days` days up to and including
    today. Days with zero activity are correctly represented as 0 —
    NOT skipped — so gaps are visible in the chart rather than silently
    compressing the timeline."""
    today = datetime.utcnow().date()
    counts = defaultdict(int)
    for ts in timestamps:
        if isinstance(ts, datetime):
            counts[ts.date()] += 1
    return [counts.get(today - timedelta(days=offset), 0) for offset in range(days - 1, -1, -1)]


def _render_chart(file_counts: list, user_counts: list, days: int) -> bytes:
    """Build the two-panel PNG chart. Runs synchronously — always call this
    via asyncio.to_thread() from the async handler, since matplotlib
    rendering is CPU-bound and would otherwise block the event loop for
    every other user's requests while it runs."""
    labels = [(datetime.utcnow().date() - timedelta(days=offset)).strftime("%m/%d")
               for offset in range(days - 1, -1, -1)]
    # Thin out x-axis labels for wider windows so they don't overlap.
    label_stride = max(1, days // 12)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 6), dpi=140)
    fig.patch.set_facecolor("#ffffff")

    for ax, data, title, color in (
        (ax1, file_counts, "Files uploaded per day", "#4C8DFF"),
        (ax2, user_counts, "New users per day", "#FFB454"),
    ):
        ax.bar(range(days), data, color=color, width=0.75)
        ax.set_title(title, fontsize=11, fontweight="bold", loc="left", color="#12151A")
        ax.set_xticks(range(0, days, label_stride))
        ax.set_xticklabels([labels[i] for i in range(0, days, label_stride)], fontsize=8, rotation=45, ha="right")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.set_facecolor("#ffffff")
        ax.tick_params(colors="#5B6472")
        ax.yaxis.grid(True, linestyle="--", alpha=0.3)
        ax.set_axisbelow(True)
        # Integer-only y-axis ticks — fractional "2.5 files" makes no sense.
        max_val = max(data) if data else 0
        if max_val <= 10:
            ax.set_yticks(range(0, max_val + 2))

    fig.tight_layout(pad=2.0)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


@Client.on_message(filters.command("analytics") & admin_filter & filters.private)
async def analytics_command(client: Client, message: Message):
    if not MATPLOTLIB_AVAILABLE:
        await message.reply(
            "📊 **/analytics needs matplotlib**, which isn't installed on "
            "this host.\n\nInstall it with:\n`pip install matplotlib`\n\n"
            "Every other command works normally without it — this is the "
            "only feature that needs it."
        )
        return

    days = _parse_days_arg(message)
    if isinstance(days, str):
        await message.reply(f"⚠️ {days}")
        return

    status = await message.reply(f"📊 Building your {days}-day analytics chart…")

    db = client.db  # type: ignore[attr-defined]
    cutoff = datetime.utcnow() - timedelta(days=days - 1)

    try:
        files = await db.get_files_since(cutoff)
        file_timestamps = [f.get("saved_at") for f in files]

        user_timestamps = []
        async for user in db.get_all_users():
            joined = user.get("joined")
            if isinstance(joined, datetime) and joined >= cutoff:
                user_timestamps.append(joined)
    except Exception as e:
        await status.edit(f"❌ Couldn't load analytics data: `{e}`")
        return

    file_counts = _bucket_by_day(file_timestamps, days)
    user_counts = _bucket_by_day(user_timestamps, days)

    try:
        # CPU-bound rendering — offload to a thread so it never blocks the
        # event loop (every other user's request would otherwise stall for
        # however long chart rendering takes).
        png_bytes = await asyncio.to_thread(_render_chart, file_counts, user_counts, days)
    except Exception as e:
        await status.edit(f"❌ Couldn't render the chart: `{e}`")
        return

    total_files = sum(file_counts)
    total_users = sum(user_counts)
    # Simple trend signal: 2nd half of the window vs the 1st half.
    half = days // 2
    files_trend = "📈" if sum(file_counts[half:]) >= sum(file_counts[:half]) else "📉"
    users_trend = "📈" if sum(user_counts[half:]) >= sum(user_counts[:half]) else "📉"

    caption = (
        f"📊 **Analytics — last {days} days**\n\n"
        f"▸ Files uploaded: **{total_files}** {files_trend}\n"
        f"▸ New users: **{total_users}** {users_trend}\n\n"
        f"_Tip: `/analytics 30` for a longer window (2–{_MAX_DAYS} days)._"
    )

    await client.send_photo(
        message.chat.id,
        photo=io.BytesIO(png_bytes),
        caption=caption,
    )
    await status.delete()
