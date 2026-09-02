"""
Web server — v8 ULTIMATE EDITION

All bugs fixed, all feature requests implemented:

  BUG FIX #2  — Download & Stream 404:
    Routes /download/<id> and /stream/<id> are now guaranteed to resolve
    correctly. The client-ready check ensures the Pyrogram client is
    connected before serving any range requests. Proper error pages
    (not generic 404) are shown for not-found / expired files.

  BUG FIX #6  — Concurrent download blocking:
    Every request is fully async. aiohttp handles N simultaneous
    connections for the same or different files without any queue.
    File chunks are streamed directly without loading into memory.

  FEATURE #3  — YouTube-like video player:
    Custom HTML5 player with: play/pause, stop, seek bar, speed control
    (0.5x–2x), volume, mute, fullscreen, Picture-in-Picture, buffering
    indicator, remaining-time display, resume-from-last-position
    (localStorage), keyboard shortcuts, mobile-friendly touch controls.

  FEATURE #4  — Backend improvements:
    • HTTP Range Requests for smooth seeking and parallel downloads
    • Pyrogram chunk-aligned offset (fast seek, no re-download from 0)
    • Resume interrupted downloads (ETag + Last-Modified + If-Range)
    • Proper MIME type detection: extension table first, with a REAL
      python-magic content-sniff fallback (reads first chunk's magic bytes)
      for files with a missing/generic extension — degrades gracefully to
      extension-only guessing if python-magic/libmagic isn't installed
    • Short-lived message cache (30 min TTL, avoids Telegram API spam)
    • File availability check before generating links
    • Backlog=512 for high concurrency acceptance

  FEATURE #7  — Wide format support:
    • MKV, AVI, MOV, WMV, ASF, FLV, F4V, 3GP/3G2, MPG/MPEG, DIVX, VOB,
      OGV, RM/RMVB, WEBM video
    • MP3, OGG, WAV, FLAC, AAC, M4A/M4B, OPUS, WMA, AMR, AIFF, MIDI audio
    • SRT/VTT/ASS/SSA subtitle sidecars recognized for player <track> use

  FEATURE #5  — UI/UX improvements:
    • Modern file preview page with glassmorphism design
    • Thumbnail/poster display for videos
    • Clean metadata display
    • Loading states while fetching
    • User-friendly error messages with Go-Home button
    • Mobile-responsive layout
"""

import asyncio
import contextlib
import logging
import os
from datetime import datetime
from email.utils import formatdate

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

from info import DB_CHANNEL, WEB_SERVER_BIND_ADDRESS, WEB_SERVER_PORT, SITE_NAME, SITE_TAGLINE, CREATOR_NAME, BOT_USERNAME
import transfer_stats
from utils import (
    humanbytes, detect_mime as _detect_mime_base, is_streamable_media,
    STREAMABLE_MSG_TYPES as STREAMABLE_TYPES, VIDEO_MIMES, AUDIO_MIMES,
)

# IMPROVEMENT (organization): caching, security middleware, and Jinja
# rendering used to all live in this one file alongside routing and the
# core streaming logic. They're now split into their own modules — this
# is a verbatim move, not a behavior change, so every name below is
# imported under its original (possibly underscore-prefixed) name to keep
# every call site elsewhere in this file unchanged.
from web.render import jinja_env as _jinja_env, render as _render
from web.cache import (
    MAGIC_AVAILABLE,
    get_cached_message as _get_cached_message,
    is_fatal_auth_error as _is_fatal_auth_error,
    sniff_mime as _sniff_mime,
    peek_sniffed_mime as _peek_sniffed_mime,
    clear_entry as _clear_cache_entry,
    sweep_message_cache,
)
from web.security import (
    security_headers_middleware,
    rate_limit_middleware,
    sweep_rate_limit_buckets,
)

# ── Media type sets ─────────────────────────────────────────────────────────
# STREAMABLE_TYPES / VIDEO_MIMES / AUDIO_MIMES now come from utils.py — the
# same tables plugins/file_handler.py uses for the bot's Telegram reply, so
# both surfaces always agree on what's streamable. See utils.py's module
# docstring for why that used to drift (and silently break .mkv streaming).

ICON_MAP = {
    "video": "🎬", "audio": "🎵", "voice": "🎙️", "document": "📄",
    "photo": "🖼️", "animation": "🎞️", "sticker": "🎭", "video_note": "📹",
}

logger = logging.getLogger(__name__)

# ── Pyrogram chunk-size math ─────────────────────────────────────────────────
# The `offset` argument to Client.stream_media() is a CHUNK INDEX, and
# Pyrogram streams in fixed 1 MB chunks. The previous implementation computed
# a *variable* power-of-two size (64 KB–1 MB) based on file size; for any file
# smaller than 1 MB that produced a chunk size that did NOT match Pyrogram's
# actual 1 MB step, so the byte->chunk seek math could land on the wrong
# offset and serve corrupted ranges. Using the real, fixed 1 MB step makes the
# seek correct for files of every size.
STREAM_CHUNK_SIZE = 1024 * 1024    # 1 MB — matches Pyrogram's stream_media step


def get_chunk_size(file_size: int) -> int:
    """Return Pyrogram's streaming chunk size (fixed 1 MB)."""
    return STREAM_CHUNK_SIZE


# ── Idle housekeeping ─────────────────────────────────────────────────────────
# IMPROVEMENT (resource use while idle): Pyrogram's own connection is already
# fully event-driven — it sits in an asyncio await on the socket and uses
# ~zero CPU with no incoming Telegram traffic, so there's nothing to tune
# there. The actual idle-RAM waste was in the caches now living in
# web/cache.py and web/security.py: message caching and per-IP rate-limit
# buckets both only shrink when a NEW request happens to touch them —
# eviction is purely lazy, triggered on write. That means after a busy
# period, if the site then goes fully idle, those caches just sit in RAM at
# whatever size they reached during the busy period — potentially for
# hours, since nothing ever prompts a shrink without fresh traffic to
# trigger it.
#
# This background sweep runs on a slow timer and proactively drops entries
# that are provably no longer useful — so the process's memory footprint
# actually comes back down while genuinely idle, instead of staying pinned
# at its peak until the next burst of traffic happens to walk through the
# same code path. The work itself is cheap (single dict pass per module,
# runs once per interval) and only touches data structures that already
# exist — no new idle CPU cost beyond one wakeup every few minutes.
_IDLE_SWEEP_INTERVAL = 5 * 60   # 5 minutes


async def _idle_housekeeping_loop():
    """Background task: sweep stale cache/rate-limit entries every
    _IDLE_SWEEP_INTERVAL. Started and cancelled automatically via aiohttp's
    on_startup/on_cleanup app hooks — see create_app() below. Delegates the
    actual sweeping to each concern's own module (web/cache.py,
    web/security.py) — this loop is purely the timer/orchestration."""
    while True:
        try:
            await asyncio.sleep(_IDLE_SWEEP_INTERVAL)
            dropped_msgs = sweep_message_cache()
            dropped_ips = sweep_rate_limit_buckets()
            if dropped_msgs or dropped_ips:
                logger.debug(
                    "Idle housekeeping: freed %d expired message(s), "
                    "%d empty rate-limit bucket(s)",
                    dropped_msgs, dropped_ips,
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            # Never let a housekeeping hiccup take down the sweep loop —
            # log and keep going on the next interval.
            logger.warning("Idle housekeeping sweep failed: %s", e)


# ── Helper: MIME type detection ──────────────────────────────────────────────
def _detect_mime(file_name: str, fallback: str = "application/octet-stream") -> str:
    """
    Guess MIME type from filename. Falls back to stored value.
    Thin wrapper over utils.detect_mime() — the extension table lives there
    now so the same broadened format coverage (mkv, ts, avi, wmv, flv, and
    many more) is shared with the bot's Telegram reply.
    """
    return _detect_mime_base(file_name, fallback=fallback)


# ── Helper: expiry ───────────────────────────────────────────────────────────
def _is_expired(file_meta: dict) -> bool:
    expires_at = file_meta.get("expires_at")
    if not expires_at:
        return False
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except Exception:
            return False
    return datetime.utcnow() > expires_at


def _format_expiry(file_meta: dict):
    expires_at = file_meta.get("expires_at")
    if not expires_at:
        return None
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except Exception:
            return None
    delta = expires_at - datetime.utcnow()
    if delta.total_seconds() <= 0:
        return "Expired"
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h remaining"
    if hours > 0:
        return f"{hours}h {minutes}m remaining"
    return f"{minutes}m remaining"


# ── Helper: stable ETag for resume support ───────────────────────────────────
def _resume_headers(file_uid: str, file_meta: dict) -> tuple:
    """
    Build a stable ETag + Last-Modified pair for this file.

    Without these, download managers and browsers can't verify that a
    paused download still refers to the same bytes when resuming —
    they either fail to resume or restart from byte 0.
    """
    etag = f'"{file_uid}-{file_meta.get("file_size", 0)}"'

    saved_at = file_meta.get("saved_at")
    if isinstance(saved_at, str):
        try:
            saved_at = datetime.fromisoformat(saved_at)
        except Exception:
            saved_at = None
    if isinstance(saved_at, datetime):
        last_modified = formatdate(saved_at.timestamp(), usegmt=True)
    else:
        last_modified = formatdate(0, usegmt=True)

    return etag, last_modified


# ── Core: streaming pump ─────────────────────────────────────────────────────
async def _pump_chunks(
    client, message, response: web.StreamResponse, request: web.Request,
    file_uid: str, start_offset_chunks: int, leading_skip: int,
    chunk_len,   # int | None
) -> int:
    """
    Stream `message` from the given chunk offset, skipping `leading_skip`
    bytes of the first chunk, writing up to `chunk_len` bytes total (or
    all remaining bytes if chunk_len is None). Returns bytes written.

    BUG FIX #6 — fully async, no blocking, handles client disconnect cleanly.

    BUG FIX #9 — actually releases Pyrogram's transfer slot on early exit.
    client.stream_media() is an async generator that holds Pyrogram's
    get_file_semaphore (size = MAX_CONCURRENT_TRANSMISSIONS) AND an open
    Telegram media session for its entire lifetime; both are only released
    in a `finally` deep inside Pyrogram that runs when the generator is
    exhausted OR explicitly closed. Almost every request we serve is a
    partial Range request (video seeking, parallel-chunk download
    managers), so `break`-ing out of the `async for` below once chunk_len
    is satisfied is the NORMAL case — but a bare `break` only abandons the
    generator, it does not close it. The semaphore slot then sits held
    until Python's GC eventually finalizes the orphaned generator, which
    is not deterministic and lags further behind under real concurrent
    load. With the default of 10 slots, abandoned generators from ordinary
    seeking/parallel downloads can pile up faster than GC reclaims them,
    exhausting every slot and hanging every *new* stream/download behind
    it — i.e. exactly the "stuck loading on concurrent transfers" bug this
    module claims to fix. Explicitly closing the generator ourselves
    guarantees the slot and session are freed the instant we're done.
    """
    bytes_skipped = 0
    bytes_written = 0

    # Speed upgrade: fetch chunks from Telegram on a background task, one
    # chunk ahead of what we're currently writing to the client socket. The
    # old version awaited stream_media() and response.write() strictly back
    # to back, so the client socket sat idle during every Telegram fetch and
    # the Telegram connection sat idle during every client write. Overlapping
    # the two (prefetch depth 2) hides one side's latency behind the other's
    # and noticeably raises effective throughput, especially on slower client
    # links or higher-latency Telegram DCs.
    media_stream = client.stream_media(message, offset=start_offset_chunks)
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    _DONE = object()

    async def _producer():
        try:
            async for chunk in media_stream:
                await queue.put(chunk)
        except Exception as e:
            await queue.put(e)
        finally:
            await queue.put(_DONE)

    producer_task = asyncio.create_task(_producer())

    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            if isinstance(item, Exception):
                logger.warning("Fetch error for %s: %s", file_uid, item)
                break

            # Stop immediately if the client has disconnected
            if request.transport is None or request.transport.is_closing():
                break

            chunk_data = bytes(item)

            # Skip leading bytes inside the first chunk (chunk-alignment remainder)
            if bytes_skipped < leading_skip:
                to_skip = leading_skip - bytes_skipped
                if len(chunk_data) <= to_skip:
                    bytes_skipped += len(chunk_data)
                    continue
                chunk_data = chunk_data[to_skip:]
                bytes_skipped = leading_skip

            # Trim to requested range length
            if chunk_len is not None:
                remaining = chunk_len - bytes_written
                if remaining <= 0:
                    break
                if len(chunk_data) > remaining:
                    chunk_data = chunk_data[:remaining]

            try:
                await response.write(chunk_data)
                bytes_written += len(chunk_data)
            except (ConnectionResetError, asyncio.CancelledError):
                break
            except Exception as e:
                logger.warning("Write error for %s: %s", file_uid, e)
                break

            if chunk_len is not None and bytes_written >= chunk_len:
                break
    finally:
        # Force-release Pyrogram's semaphore slot + media session right now,
        # regardless of which branch above we exited through, instead of
        # waiting on garbage collection to close the generator for us.
        producer_task.cancel()
        try:
            await producer_task
        except Exception:
            pass
        try:
            await media_stream.aclose()
        except Exception:
            pass

    return bytes_written


# ── Route: /stream/<id> and /download/<id> ───────────────────────────────────
async def stream_handler(request: web.Request):
    """
    Serves both /stream/<id> (inline) and /download/<id> (attachment).

    BUG FIX #2  — Proper 404/410 error pages instead of generic server errors.
    BUG FIX #4  — HTTP Range Requests, ETag, resume support, chunk-aligned seek.
    BUG FIX #6  — Fully async, concurrent-safe, no download queue.
    """
    file_uid = request.match_info["file_uid"]
    client   = request.app["client"]
    db       = request.app["db"]

    # ── File availability check ──
    file_meta = await db.get_file(file_uid)
    if not file_meta:
        return web.Response(
            status=404, content_type="text/html",
            text=_render(
                "error_page.html",
                title="File Not Found",
                message=(
                    "This file does not exist or has been deleted by the uploader. "
                    "Please ask them to generate a new link."
                ),
                code=404,
            ),
        )

    if _is_expired(file_meta):
        return web.Response(
            status=410, content_type="text/html",
            text=_render(
                "error_page.html",
                title="Link Expired",
                message="This file link has expired and is no longer accessible.",
                code=410,
            ),
        )

    msg_id    = int(file_meta["msg_id"])
    file_size = int(file_meta.get("file_size") or 0)
    file_name = file_meta.get("file_name") or "file"

    # Re-detect MIME type from filename for accuracy (fixes .mkv being served
    # as application/octet-stream, which breaks browser video playback).
    stored_mime = file_meta.get("mime_type", "application/octet-stream")
    mime_type   = _detect_mime(file_name, fallback=stored_mime)

    is_download  = request.path.startswith("/download/")
    etag, last_modified = _resume_headers(file_uid, file_meta)

    # Sanitize the filename for the Content-Disposition header. A raw filename
    # containing newlines/control chars or quotes can make aiohttp raise while
    # building headers (turning a download into a 500) or allow header
    # injection. We send an ASCII-safe fallback plus an RFC 5987 filename*.
    #
    # BUG FIX (download creates an extra folder): '/' and '\' were NOT being
    # stripped here. Browsers treat a slash inside a downloaded filename as a
    # path separator — Chrome in particular will create subfolders inside
    # the user's Downloads directory to match (e.g. a file whose Telegram
    # name happened to contain "Season 1/Episode 3.mp4" would download into
    # a new "Season 1" folder instead of saving directly). Replacing slashes
    # with "_" here means a real folder can never be created, no matter what
    # the original file name contains.
    from urllib.parse import quote
    ascii_name = "".join(
        c if (32 <= ord(c) < 127 and c not in '"\\/') else "_" for c in file_name
    ).strip() or "file"
    encoded_name = quote(file_name.replace("/", "_").replace("\\", "_"), safe="")

    # ── 304 Not Modified ──
    if request.headers.get("If-None-Match") == etag:
        return web.Response(
            status=304,
            headers={"ETag": etag, "Last-Modified": last_modified},
        )

    range_header = request.headers.get("Range")

    # ── If-Range: only honor Range if resource is unchanged ──
    if_range = request.headers.get("If-Range")
    if if_range and if_range not in (etag, last_modified):
        range_header = None

    range_start = 0
    range_end   = max(file_size - 1, 0) if file_size else 0

    if range_header and file_size:
        try:
            rng   = range_header.replace("bytes=", "")
            parts = rng.split("-")
            range_start = int(parts[0]) if parts[0] else 0
            range_end   = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
            range_start = max(0, range_start)
            range_end   = min(range_end, file_size - 1)
            if range_start > range_end:
                range_start, range_end = 0, file_size - 1
        except Exception:
            range_start, range_end = 0, file_size - 1

    chunk_len  = (range_end - range_start + 1) if file_size else None
    disposition = "attachment" if is_download else "inline"

    headers = {
        "Content-Type":        mime_type,
        "Accept-Ranges":       "bytes",
        "Content-Disposition": (
            f'{disposition}; filename="{ascii_name}"; '
            f"filename*=UTF-8''{encoded_name}"
        ),
        "Cache-Control":       "no-cache",
        "ETag":                etag,
        "Last-Modified":       last_modified,
        "X-Content-Type-Options": "nosniff",
    }
    if file_size:
        headers["Content-Length"] = str(chunk_len)
        headers["Content-Range"]  = f"bytes {range_start}-{range_end}/{file_size}"

    status = 206 if (range_header and file_size) else 200

    # ── HEAD request — headers only, no body ──
    if request.method == "HEAD":
        return web.Response(status=status, headers=headers)

    # ── Pre-flight: confirm the source message still exists in DB_CHANNEL ──
    # If the uploader (or an admin) deleted the stored message, streaming would
    # otherwise begin with a 200 and then silently produce 0 bytes — the browser
    # shows a "broken"/endless download. Detect it up-front and return a clean
    # 404 error page instead (this is part of the download/stream 404 fix).
    try:
        preflight = await _get_cached_message(client, msg_id)
    except Exception as e:
        # BUG FIX (AUTH_BYTES_INVALID surfaced as a raw 500): this runs
        # BEFORE response.prepare(), i.e. before any HTTP headers are sent,
        # so on a fatal/broken Telegram session we can still return a clean
        # error page instead of an unhandled exception turning into aiohttp's
        # generic 500. A fatal auth error means retrying won't help until the
        # bot process restarts with a fresh session — see _is_fatal_auth_error.
        if _is_fatal_auth_error(e):
            logger.error(
                "Fatal Telegram auth error resolving message %s — session "
                "needs a restart to recover: %s", msg_id, e,
            )
            return web.Response(
                status=503, content_type="text/html",
                text=_render(
                    "error_page.html",
                    title="Temporarily Unavailable",
                    message=(
                        "The bot's connection to Telegram needs to restart to "
                        "recover. Please try again in a minute."
                    ),
                    code=503,
                ),
            )
        logger.error("Error resolving message %s for %s: %s", msg_id, file_uid, e)
        preflight = None

    if not preflight or getattr(preflight, "empty", False):
        logger.error("Source message %s missing in DB_CHANNEL (file %s)", msg_id, file_uid)
        return web.Response(
            status=404, content_type="text/html",
            text=_render(
                "error_page.html",
                title="File No Longer Available",
                message=(
                    "The stored copy of this file was removed and can no longer "
                    "be served. Please ask the uploader to generate a new link."
                ),
                code=404,
            ),
        )

    # FEATURE #4 — python-magic fallback: the extension-based guess couldn't
    # tell us anything useful (missing/generic extension), so sniff the
    # file's real magic bytes before we commit to a Content-Type header.
    # HEAD requests skip this since headers were already sent above them.
    if mime_type == "application/octet-stream" and MAGIC_AVAILABLE:
        real_mime = await _sniff_mime(client, preflight, file_uid)
        if real_mime and real_mime != "application/octet-stream":
            mime_type = real_mime
            headers["Content-Type"] = mime_type

    response = web.StreamResponse(status=status, headers=headers)
    try:
        await response.prepare(request)
    except Exception as e:
        logger.warning("Could not prepare response for %s: %s", file_uid, e)
        return response

    # Stat counters (fire-and-forget).
    # Only count once per transfer: a single download/seek opens many parallel
    # range requests (range_start > 0), which previously inflated the counters
    # by 10-100x. Count only the opening request (no Range, or Range from 0).
    if range_start == 0:
        try:
            stat_key = "downloads_served" if is_download else "streams_served"
            await db.increment_stat(stat_key)
            # Per-file popularity counter shown on the file page.
            await db.increment_file_stat(
                file_uid, "dl_count" if is_download else "stream_count"
            )
        except Exception:
            pass

    # ── BUG FIX #4 — chunk-aligned fast seek ──
    # Convert byte offset → pyrogram chunk index + leftover bytes.
    # This avoids re-streaming gigabytes of data just to throw it away.
    if file_size:
        csize          = get_chunk_size(file_size)
        offset_chunks  = range_start // csize
        leading_skip   = range_start - (offset_chunks * csize)
    else:
        offset_chunks  = 0
        leading_skip   = 0

    transfer_stats.transfer_started()
    fatal_auth_hit = False
    try:
        bytes_written = 0
        for attempt in range(2):
            # On 2nd attempt, force-refresh the cached message —
            # a stale file_reference is the most common cause of a
            # stream that silently produces 0 bytes.
            try:
                message = await _get_cached_message(
                    client, msg_id, force_refresh=(attempt == 1)
                )
            except Exception as e:
                # BUG FIX: get_messages() can itself raise AUTH_BYTES_INVALID
                # (this is the exact call in the reported traceback). Handle
                # it the same way as a fatal error hit inside _pump_chunks —
                # log once, stop retrying — instead of letting it propagate
                # to the generic outer "Stream error" handler below, which
                # would obscure that it's a fatal, non-retryable auth issue.
                if _is_fatal_auth_error(e):
                    logger.error(
                        "Fatal Telegram auth error fetching message %s for "
                        "%s — session needs a restart to recover: %s",
                        msg_id, file_uid, e,
                    )
                    fatal_auth_hit = True
                    bytes_written = 0
                    break
                logger.warning(
                    "Could not refresh message %s for %s (attempt %d): %s",
                    msg_id, file_uid, attempt, e,
                )
                bytes_written = 0
                break
            if not message or message.empty:
                logger.error("Message %s not found in DB_CHANNEL", msg_id)
                break

            try:
                bytes_written = await _pump_chunks(
                    client, message, response, request,
                    file_uid, offset_chunks, leading_skip, chunk_len,
                )
            except Exception as e:
                # BUG FIX (AUTH_BYTES_INVALID retry storm): a fatal auth
                # error means the Telegram session is broken — retrying
                # from offset 0 (below) would just hit the exact same
                # error again immediately, filling the log with duplicate
                # tracebacks while the client sits there getting nothing.
                # Log it ONCE at ERROR (it needs attention — the process
                # likely needs a restart to get a clean session) and stop
                # retrying this request outright.
                if _is_fatal_auth_error(e):
                    logger.error(
                        "Fatal Telegram auth error streaming %s — session "
                        "needs a restart to recover: %s", file_uid, e,
                    )
                    fatal_auth_hit = True
                    bytes_written = 0
                    break
                logger.warning(
                    "stream_media error for %s (attempt %d): %s",
                    file_uid, attempt, e,
                )
                bytes_written = 0

            if bytes_written > 0:
                break

            if request.transport is None or request.transport.is_closing():
                break

            # Retry from offset 0 with only a skip (safer fallback)
            logger.warning(
                "No data for %s (attempt %d), retrying from offset 0",
                file_uid, attempt,
            )
            offset_chunks = 0
            leading_skip  = range_start

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Stream error for %s: %s", file_uid, e)
    finally:
        transfer_stats.transfer_finished(bytes_written if range_start == 0 else 0)

    # NOTE: by this point response.prepare() has already sent status 200/206
    # headers to the client, so a fatal auth error hit mid-stream (rather than
    # during the preflight check above) can no longer be swapped for a 503 —
    # the client already received a success status. The best we can do here
    # is stop cleanly via write_eof() below and rely on the ERROR-level log
    # from _is_fatal_auth_error above to flag that the session needs a
    # restart. fatal_auth_hit is kept only for that log signal's context.
    _ = fatal_auth_hit

    # ── Clear server-side temp/cache data on a fully-completed transfer ──────
    # Once a download or stream has been served all the way through to the
    # end of the file (not just a partial seek/range chunk), the short-lived
    # in-memory caches for THIS file are no longer earning their keep for
    # this particular client — so free them immediately rather than waiting
    # for their normal 30-minute TTL / LRU eviction.
    #
    # Important: this only clears in-memory PERFORMANCE caches (owned by
    # web/cache.py) on this server process. It does NOT touch the actual
    # file in the Telegram DB Channel, the database record, or the
    # shareable link — the next request for this file simply re-populates
    # the caches from a fresh Telegram API call, exactly as if this were
    # the very first request. Nothing breaks; this is pure cleanup.
    reached_end_of_file = bool(file_size) and (range_end >= file_size - 1)
    served_something     = bytes_written > 0
    if reached_end_of_file and served_something and (request.transport is not None):
        _clear_cache_entry(msg_id, file_uid)
        logger.debug(
            "Cleared server-side cache for %s after full %s completed",
            file_uid, "download" if is_download else "stream",
        )

    try:
        await response.write_eof()
    except Exception:
        pass

    return response


async def download_handler(request: web.Request):
    """Alias — /download/<id> is served by stream_handler (sets attachment disposition)."""
    return await stream_handler(request)


# ── Route: /thumbnail/<id> ────────────────────────────────────────────────────
async def thumbnail_handler(request: web.Request):
    """Generate a JPEG thumbnail for videos/photos from Telegram."""
    file_uid = request.match_info["file_uid"]
    client   = request.app["client"]
    db       = request.app["db"]

    file_meta = await db.get_file(file_uid)
    if not file_meta:
        return web.Response(status=404)

    msg_id = int(file_meta["msg_id"])
    ftype  = file_meta.get("type", "document")

    if ftype not in ("video", "photo", "animation", "video_note"):
        return web.Response(status=204)   # No content — not a visual file

    try:
        message = await _get_cached_message(client, msg_id)
        if not message or message.empty:
            return web.Response(status=204)

        # PERF BUG FIX: the old code did download_media(message, ...) which
        # downloads the ENTIRE media (a full multi-GB video!) just to render a
        # thumbnail — slow and a memory bomb. Instead, download the tiny
        # Telegram-generated thumbnail (a few KB). Fall back to the photo
        # itself only for photos (which are already small).
        media = getattr(message, ftype, None)
        thumb_file_id = None

        thumbs = getattr(media, "thumbs", None)
        if thumbs:
            # thumbs are ordered small -> large; the first is the smallest.
            thumb_file_id = thumbs[0].file_id
        elif ftype == "photo" and media is not None:
            thumb_file_id = media.file_id

        if not thumb_file_id:
            return web.Response(status=204)

        data = await client.download_media(thumb_file_id, in_memory=True)
        if data:
            return web.Response(
                body=bytes(data.getbuffer()),
                content_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except Exception as e:
        logger.warning("Thumbnail error for %s: %s", file_uid, e)

    return web.Response(status=204)


# ── Route: /file/<id> ─────────────────────────────────────────────────────────
async def file_page_handler(request: web.Request):
    """
    Renders the modern file preview page.

    FEATURE #3  — YouTube-like player embedded in the page.
    FEATURE #5  — Modern UI, metadata display, loading states, error messages.
    """
    file_uid = request.match_info["file_uid"]
    db       = request.app["db"]
    base_url = request.app.get("base_url", "")

    file_meta = await db.get_file(file_uid)
    if not file_meta:
        return web.Response(
            status=404, content_type="text/html",
            text=_render(
                "error_page.html",
                title="File Not Found",
                message=(
                    "This file does not exist or has been deleted by the uploader. "
                    "Please ask them to generate a new link."
                ),
                code=404,
            ),
        )

    if _is_expired(file_meta):
        return web.Response(
            status=410, content_type="text/html",
            text=_render(
                "error_page.html",
                title="Link Expired",
                message="This file link has expired. Please request a new link from the uploader.",
                code=410,
            ),
        )

    # Count this page view (best-effort) and surface popularity numbers.
    try:
        await db.increment_file_stat(file_uid, "view_count")
    except Exception:
        pass
    view_count   = int(file_meta.get("view_count", 0)) + 1
    stream_count = int(file_meta.get("stream_count", 0))
    dl_count     = int(file_meta.get("dl_count", 0))

    file_name  = file_meta.get("file_name") or "Unknown File"
    file_size  = humanbytes(int(file_meta.get("file_size") or 0))
    stored_mime = file_meta.get("mime_type", "application/octet-stream")
    mime_type  = _detect_mime(file_name, fallback=stored_mime)
    # Reuse a mime type already discovered by a prior /stream or /download hit
    # (see web/cache.py's sniff_mime) instead of re-guessing blind for oddly-
    # named files.
    sniffed = _peek_sniffed_mime(file_uid)
    if mime_type == "application/octet-stream" and sniffed:
        mime_type = sniffed
    ftype      = file_meta.get("type", "document")
    expiry_str = _format_expiry(file_meta)

    is_streamable = is_streamable_media(ftype, mime_type=mime_type, file_name=file_name)
    is_video      = mime_type in VIDEO_MIMES or ftype in ("video", "animation", "video_note")
    is_audio      = mime_type in AUDIO_MIMES or ftype in ("audio", "voice")
    has_thumbnail = ftype in ("video", "photo", "animation")

    stream_url   = f"{base_url}/stream/{file_uid}"
    download_url = f"{base_url}/download/{file_uid}"
    thumb_url    = f"{base_url}/thumbnail/{file_uid}" if has_thumbnail else None
    icon = ICON_MAP.get(ftype, "📁")

    # FEATURE #3 upgrade — wire up the subtitle sidecar support that FEATURE
    # #7 already recognizes (SRT/VTT/ASS/SSA MIME detection existed, but
    # nothing ever attached a track to the player). Only WebVTT can be used
    # natively in an HTML <track> element (browsers don't parse .srt/.ass),
    # so we look for a sibling .vtt uploaded in the same batch as this
    # video — preferring one whose filename stem matches, falling back to
    # the first .vtt in the batch. Best-effort: any failure here just means
    # no captions are offered, never a broken page.
    subtitle_url = None
    if is_video:
        subtitle_url = await _find_sibling_subtitle(db, file_meta, file_name)

    # SEO / Open Graph context
    canonical_url = f"{base_url}/file/{file_uid}" if base_url else None
    page_desc = f"{file_name} ({file_size}) — stream instantly or download via {SITE_NAME}."

    html = _render(
        "file_page.html",
        file_name=file_name, file_size=file_size, mime_type=mime_type,
        icon=icon, stream_url=stream_url, download_url=download_url,
        thumb_url=thumb_url, is_streamable=is_streamable, is_video=is_video,
        is_audio=is_audio, has_thumbnail=has_thumbnail,
        expiry_str=expiry_str, file_uid=file_uid,
        view_count=view_count, stream_count=stream_count, dl_count=dl_count,
        page_title=f"{file_name} — {SITE_NAME}",
        page_desc=page_desc, canonical_url=canonical_url,
        og_image=thumb_url, og_type="video.other" if is_video else "website",
        subtitle_url=subtitle_url,
    )
    return web.Response(text=html, content_type="text/html")


async def _find_sibling_subtitle(db, file_meta: dict, file_name: str) -> "str | None":
    """Look for a .vtt file uploaded in the same batch as `file_meta`.

    Returns a /stream/<uid> URL (already served with the correct
    text/vtt Content-Type by _detect_mime) or None if no batch, no sibling
    subtitle, or any lookup error occurs.
    """
    batch_id = file_meta.get("batch_id")
    if not batch_id:
        return None
    try:
        batch = await db.get_batch(batch_id)
        if not batch:
            return None
        stem = os.path.splitext(file_name)[0].lower()
        candidates = []
        for sib_uid in batch.get("files", []):
            sib = await db.get_file(sib_uid)
            if not sib:
                continue
            sib_name = sib.get("file_name") or ""
            if os.path.splitext(sib_name)[1].lower() != ".vtt":
                continue
            candidates.append((sib_uid, os.path.splitext(sib_name)[0].lower()))
        if not candidates:
            return None
        # Prefer a same-stem match (e.g. movie.mkv + movie.vtt) over an
        # arbitrary .vtt elsewhere in the batch.
        for sib_uid, sib_stem in candidates:
            if sib_stem == stem:
                return f"/stream/{sib_uid}"
        return f"/stream/{candidates[0][0]}"
    except Exception as e:
        logger.debug("Subtitle sidecar lookup failed: %s", e)
        return None


# ── Route: /batch/<id> ────────────────────────────────────────────────────────
async def batch_page_handler(request: web.Request):
    batch_id = request.match_info["batch_id"]
    db       = request.app["db"]
    base_url = request.app.get("base_url", "")

    batch = await db.get_batch(batch_id)
    if not batch:
        return web.Response(
            status=404, content_type="text/html",
            text=_render(
                "error_page.html",
                title="Batch Not Found",
                message="This batch link is invalid or has been removed.",
                code=404,
            ),
        )

    files_meta = []
    for fuid in batch.get("files", []):
        fm = await db.get_file(fuid)
        if fm:
            fm["_uid"]          = fuid
            fm["_download_url"] = f"{base_url}/download/{fuid}"
            fm["_stream_url"]   = f"{base_url}/stream/{fuid}"
            fm["_page_url"]     = f"{base_url}/file/{fuid}"
            fm["_thumb_url"]    = f"{base_url}/thumbnail/{fuid}" if fm.get("type") in ("video", "photo", "animation") else None
            fm["_size_human"]   = humanbytes(int(fm.get("file_size") or 0))
            fm["_icon"]         = ICON_MAP.get(fm.get("type", "document"), "📁")
            fm["_streamable"]   = fm.get("type") in STREAMABLE_TYPES
            files_meta.append(fm)

    canonical_url = f"{base_url}/batch/{batch_id}" if base_url else None
    html = _render(
        "batch_page.html",
        batch_id=batch_id, files=files_meta,
        total=len(files_meta), status=batch.get("status", "?"),
        page_title=f"Batch ({len(files_meta)} files) — {SITE_NAME}",
        page_desc=f"A shared batch of {len(files_meta)} links. Stream individually or download all via {SITE_NAME}.",
        canonical_url=canonical_url,
    )
    return web.Response(text=html, content_type="text/html")


# ── Route: /info/<id> (JSON API) ──────────────────────────────────────────────
async def info_handler(request: web.Request):
    file_uid  = request.match_info["file_uid"]
    db        = request.app["db"]
    file_meta = await db.get_file(file_uid)
    if not file_meta:
        return web.json_response({"error": "not found"}, status=404)
    safe = {k: v for k, v in file_meta.items() if k not in ("_id", "file_id")}
    safe["file_uid"]     = file_uid
    safe["expired"]      = _is_expired(file_meta)
    safe["expiry_label"] = _format_expiry(file_meta)
    # BUG FIX — web.json_response() does NOT accept a `default=` kwarg; passing
    # it raised TypeError -> HTTP 500 on every /info call. Datetimes and other
    # non-JSON values are serialized via a custom dumps that sets default=str.
    import functools, json as _json
    return web.json_response(safe, dumps=functools.partial(_json.dumps, default=str))


# ── Route: / (index) ─────────────────────────────────────────────────────────
async def index_handler(request: web.Request):
    base_url = request.app.get("base_url", "")
    html = _render(
        "index.html",
        canonical_url=(base_url + "/") if base_url else None,
    )
    return web.Response(text=html, content_type="text/html")


# ── Route: /health (Railway / uptime checks) ─────────────────────────────────
async def health_handler(request: web.Request):
    """Lightweight liveness probe — never touches Telegram or the DB."""
    return web.json_response({"status": "ok"})


# ── Route: /robots.txt ───────────────────────────────────────────────────────
async def robots_handler(request: web.Request):
    base_url = request.app.get("base_url", "")
    lines = [
        "User-agent: *",
        "Allow: /$",
        "Disallow: /download/",
        "Disallow: /stream/",
        "Disallow: /info/",
    ]
    if base_url:
        lines.append(f"Sitemap: {base_url}/sitemap.xml")
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


# ── Route: /sitemap.xml ──────────────────────────────────────────────────────
async def sitemap_handler(request: web.Request):
    """Minimal sitemap. Individual file links are private/unguessable and are
    intentionally excluded — only the public landing page is listed."""
    base_url = request.app.get("base_url", "") or str(request.url.with_path("/")).rstrip("/")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{base_url}/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>\n"
        "</urlset>\n"
    )
    return web.Response(text=xml, content_type="application/xml")


# ── App factory ───────────────────────────────────────────────────────────────
def create_app(client, db, base_url: str = "") -> web.Application:
    app = web.Application(
        client_max_size=4 * 1024 ** 3,
        middlewares=[security_headers_middleware, rate_limit_middleware],
    )
    app["client"]   = client
    app["db"]       = db
    app["base_url"] = base_url.rstrip("/")

    # Now that we actually have the live Bot instance, resolve its real
    # username (set in bot.py's start() from get_me(), i.e. Telegram's own
    # answer — not the possibly stale/unset BOT_USERNAME env var) so every
    # page's "Open the bot" link points at the right place. Falls back to
    # the env var only if, for some reason, the live value isn't set yet.
    _jinja_env.globals["bot_username"] = (
        getattr(client, "username", None) or BOT_USERNAME or ""
    )

    # BUG FIX #2 — all routes properly registered
    app.router.add_get("/",                    index_handler)
    app.router.add_get("/health",              health_handler)
    app.router.add_get("/robots.txt",          robots_handler)
    app.router.add_get("/sitemap.xml",         sitemap_handler)
    # NOTE: aiohttp's add_get(..., allow_head=True) ALREADY registers a HEAD
    # route for the same path, so a separate add_head() raised
    # "method HEAD is already registered" at startup. HEAD is handled inside
    # stream_handler (it returns headers only), so allow_head=True is all we need.
    app.router.add_get("/stream/{file_uid}",   stream_handler)   # GET + HEAD (resume checks)
    app.router.add_get("/download/{file_uid}", download_handler) # GET + HEAD (download managers)
    app.router.add_get("/file/{file_uid}",     file_page_handler)
    app.router.add_get("/thumbnail/{file_uid}",thumbnail_handler)
    app.router.add_get("/batch/{batch_id}",    batch_page_handler)
    app.router.add_get("/info/{file_uid}",     info_handler)

    # Static assets (CSS/JS/favicon) with long-lived browser caching.
    _static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(_static_dir):
        app.router.add_static("/static/", _static_dir, append_version=True)

    # Idle housekeeping (see _idle_housekeeping_loop above): started/stopped
    # via aiohttp's own app lifecycle signals rather than threading a task
    # reference through bot.py's shutdown sequence — on_cleanup already
    # fires exactly once, at the right time, whenever runner.cleanup() runs.
    async def _start_housekeeping(app):
        app["housekeeping_task"] = asyncio.create_task(_idle_housekeeping_loop())

    async def _stop_housekeeping(app):
        task = app.get("housekeeping_task")
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.on_startup.append(_start_housekeeping)
    app.on_cleanup.append(_stop_housekeeping)

    return app


async def start_web_server(client, db, base_url: str = ""):
    app    = create_app(client, db, base_url=base_url)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    # BUG FIX #6 — backlog=512 allows many simultaneous connections to be
    # accepted by the OS without dropping them while we're handling others.
    site = web.TCPSite(
        runner,
        WEB_SERVER_BIND_ADDRESS,
        WEB_SERVER_PORT,
        backlog=512,
    )
    await site.start()
    logger.info(
        "Web server on http://%s:%s",
        WEB_SERVER_BIND_ADDRESS, WEB_SERVER_PORT,
    )
    return runner
