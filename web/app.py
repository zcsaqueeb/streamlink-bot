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
import time
from datetime import datetime, timezone
from email.utils import formatdate

from aiohttp import web

from info import DB_CHANNEL, WEB_SERVER_BIND_ADDRESS, WEB_SERVER_PORT, SITE_NAME, SITE_TAGLINE, CREATOR_NAME, BOT_USERNAME
import info as cfg
import transfer_stats
from utils import (
    humanbytes, detect_mime as _detect_mime_base, is_streamable_media,
    expiry_datetime, is_expired as _is_expired,
    STREAMABLE_MSG_TYPES as STREAMABLE_TYPES, VIDEO_MIMES, AUDIO_MIMES,
)

# IMPROVEMENT (organization): caching, security middleware, and Jinja
# rendering used to all live in this one file alongside routing and the
# core streaming logic. They're now split into their own modules — this
# is a verbatim move, not a behavior change, so every name below is
# imported under its original (possibly underscore-prefixed) name to keep
# every call site elsewhere in this file unchanged.
from web.render import jinja_env as _jinja_env, render as _render
from web import media
from web import locks as _locks
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
    # Reused for the per-link password attempt gate. Importing it rather than
    # re-deriving the IP: the X-Forwarded-For handling in there is exactly the
    # part that must not be duplicated wrong (it used to trust the header
    # outright, which made the limiter bypassable).
    _client_ip,
    # Same trust decision, applied to X-Forwarded-Proto: whether to mark the
    # unlock cookie Secure.
    client_is_https,
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
_ORPHAN_SWEEP_EVERY = 12        # once an hour (12 × 5 min)


async def _idle_housekeeping_loop(db=None):
    """Background task: sweep stale cache/rate-limit entries every
    _IDLE_SWEEP_INTERVAL. Started and cancelled automatically via aiohttp's
    on_startup/on_cleanup app hooks — see create_app() below. Delegates the
    actual sweeping to each concern's own module (web/cache.py,
    web/security.py) — this loop is purely the timer/orchestration.

    `db` additionally enables the hourly FFmpeg track-cache orphan sweep
    (web/media.py's sweep_orphans), which needs the authoritative file
    records to tell live cached tracks from leftovers of deleted files."""
    sweeps = 0
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
            sweeps += 1
            if db is not None and sweeps % _ORPHAN_SWEEP_EVERY == 0:
                # Expired records first: their cached tracks become orphans
                # the moment the record goes, so the very same pass can
                # reclaim the disk.
                expired = await db.delete_expired_files()
                if expired:
                    logger.info("Removed %d expired file record(s)", expired)
                await media.sweep_orphans(db)
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
# is_expired()/expiry_datetime() come from utils.py, where the bot's own reply
# path (plugins/start.py) and /sub use the same answer as these routes. The
# old local copy of the parse is what let a tz-aware `expires_at` raise
# TypeError against datetime.utcnow() — a 500 where a 410 was correct.
def _format_expiry(file_meta: dict):
    expires_at = expiry_datetime(file_meta)
    if not expires_at:
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


# ── Helper: password gate ────────────────────────────────────────────────────
def _lock_page(request, *, error: str = "", retry_seconds: int = 0,
               status: int = 0) -> web.Response:
    """Render the password prompt for a locked link."""
    file_uid = request.match_info["file_uid"]
    headers = {"Retry-After": str(retry_seconds)} if retry_seconds else None
    return web.Response(
        status=status or (429 if retry_seconds else 401),
        content_type="text/html",
        headers=headers,
        text=_render(
            "lock_page.html",
            file_uid=file_uid,
            error=error,
            page_title=f"Protected link — {SITE_NAME}",
            page_desc="This link is protected by a password.",
            robots="noindex, nofollow",
            canonical_url=None,
        ),
    )


def _locked_response(request, file_meta: dict, *, webpage: bool = False,
                     api: bool = False) -> "web.Response | None":
    """
    Refuse a request for a locked file this client hasn't unlocked. Returns
    None when the request may proceed.

    Everything that can hand over the file's bytes is gated (/stream,
    /download, /subs, /media, /thumbnail), and so are the two that hand over
    its *metadata*: /file's page title and /info's JSON both give away the
    filename, which is the one thing a password is really protecting on a link
    whose uid is already unguessable.
    """
    if not _locks.needs_password(request, file_meta):
        return None
    if api:
        return web.json_response(
            {"error": "locked", "message": "This file is password protected."},
            status=401,
        )
    if webpage:
        return _lock_page(request)
    return web.Response(
        status=401, content_type="text/plain",
        text="This file is password protected.",
        headers={"X-Content-Type-Options": "nosniff"},
    )


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
        # saved_at is naive UTC throughout this app. datetime.timestamp()
        # interprets a naive value as LOCAL time, so on any non-UTC host the
        # emitted Last-Modified was offset by the whole TZ difference —
        # making conditional If-Range/If-Modified-Since resume requests never
        # match and forcing download managers to restart from byte 0.
        # Declaring it UTC first makes the header correct everywhere.
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
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

    denied = _locked_response(request, file_meta)
    if denied:
        return denied

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
            rng = range_header.replace("bytes=", "").strip()
            if rng.startswith("-"):
                # BUG FIX: a suffix range means "the LAST N bytes", not
                # "bytes 0..N". Splitting on "-" treated it as the latter, so
                # a client asking to resume the tail of a file got the head
                # instead — corrupting resumed downloads and the final-chunk
                # requests some players make.
                suffix = int(rng[1:])
                if suffix <= 0:
                    raise ValueError("empty suffix range")
                range_start = max(file_size - suffix, 0)
                range_end = file_size - 1
            else:
                head, _, tail = rng.partition("-")
                range_start = int(head) if head else 0
                range_end = int(tail) if tail else file_size - 1
            range_start = max(0, range_start)
            range_end = min(range_end, file_size - 1)
            if range_start > range_end or range_start >= file_size:
                # RFC 7233: an unsatisfiable range must be refused with 416,
                # not quietly answered as the whole file — that silently
                # ignores a client's resume position. Browsers handle 416 by
                # refetching from the start, which is the correct recovery.
                return web.Response(
                    status=416,
                    headers={
                        "Content-Range": f"bytes */{file_size}",
                        "Accept-Ranges": "bytes",
                        "ETag": etag,
                        "Last-Modified": last_modified,
                    },
                )
        except (ValueError, IndexError):
            range_start, range_end = 0, file_size - 1

    chunk_len  = (range_end - range_start + 1) if file_size else None
    disposition = "attachment" if is_download else "inline"
    status = 206 if (range_header and file_size) else 200

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
        # BUG FIX: Content-Range was sent on every response that knew the
        # file size, including plain full-file 200s. It is only legal on 206
        # (and 416) — a 200 carrying it is malformed, and resuming clients
        # that trust it (curl -C -, download managers, some Safari builds)
        # can splice the advertised window onto the full body and write a
        # corrupt file.
        if status == 206:
            headers["Content-Range"] = f"bytes {range_start}-{range_end}/{file_size}"

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

    # The poster image reveals as much about a locked file as its name does —
    # it's usually the first frame of the thing being protected.
    denied = _locked_response(request, file_meta)
    if denied:
        return denied

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


# ── Routes: extracted subtitle / audio tracks ────────────────────────────────
# WebVTT and the remuxed audio files produced by web/media.py live on this
# server's disk (MEDIA_CACHE_DIR), not in Telegram — so they're served
# straight from the filesystem. That means no Telegram round-trip, no
# transfer-slot pressure, and the browser's native Range handling works for
# free via sendfile.
def _serve_track_file(path: str, content_type: str,
                      cache_control: str = "public, max-age=31536000"):
    """Serve an already-extracted track straight from disk.

    These files live on this server (not in Telegram), so aiohttp's own
    sendfile + Range/ETag/If-Range handling applies — no semaphore, no
    chunk pump, and audio seeking works exactly like a static file.
    """
    if not os.path.isfile(path):
        return web.Response(status=404, content_type="text/plain", text="Track not ready")
    return web.FileResponse(
        path,
        headers={
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Cache-Control": cache_control,
            "X-Content-Type-Options": "nosniff",
        },
    )


_AUDIO_CONTENT_TYPES = {
    "m4a": "audio/mp4", "mp3": "audio/mpeg", "oga": "audio/ogg",
    "ogg": "audio/ogg", "flac": "audio/flac", "wav": "audio/wav",
}

# Ceiling on a sidecar subtitle that /subs/ downloads and converts in memory on
# demand. /subs/ isn't rate-limited (it's a normal part of every page view), so
# this is the only thing standing between one huge "subtitle" upload and a
# repeated multi-gigabyte read into RAM.
_MAX_SIDECAR_BYTES = 20 * 1024 * 1024


def _track_index(request: web.Request) -> "int | None":
    try:
        idx = int(request.match_info.get("index", ""))
    except ValueError:
        return None
    return idx if 0 <= idx < 256 else None


async def subtitle_handler(request: web.Request):
    """GET /subs/<file_uid>/<index> → WebVTT for one subtitle track."""
    file_uid = request.match_info["file_uid"]
    db = request.app["db"]
    idx = _track_index(request)
    if idx is None:
        return web.Response(status=404)

    file_meta = await db.get_file(file_uid)
    if not file_meta:
        return web.Response(status=404)

    denied = _locked_response(request, file_meta)
    if denied:
        return denied

    manifest = file_meta.get("media") or {}
    entry = next((s for s in manifest.get("subtitles", [])
                  if s.get("index") == idx), None)

    if entry is None and idx >= 100:
        # Sidecar subtitle: discovered at page-render time rather than stored
        # on the record, so re-resolve it the same way here. Cheap (one batch
        # lookup, no Telegram calls) and keeps sidecar tracks working without
        # a write-back on every page view.
        entry = next((s for s in await _collect_sidecar_subtitles(
                          db, file_meta, file_meta.get("file_name") or "", file_uid)
                      if s.get("index") == idx), None)

    if not entry:
        return web.Response(status=404)

    vtt_path = media.track_dir(file_uid) / f"sub{idx}.vtt"
    if vtt_path.is_file():
        return _serve_track_file(str(vtt_path), "text/vtt; charset=utf-8")

    # Sidecar route: the subtitle arrived as its own uploaded file next to
    # the video. It may be .srt (browsers only parse VTT), so convert and
    # cache it once here.
    return await _serve_sidecar_subtitle(request, db, file_uid, file_meta, entry, idx)


async def _serve_sidecar_subtitle(request, db, file_uid, file_meta, entry, idx):
    client = request.app["client"]
    src_uid = entry.get("src_uid")
    if not src_uid:
        return web.Response(status=404)
    src_meta = await db.get_file(src_uid)
    if not src_meta:
        return web.Response(status=404)
    if _locks.is_locked(src_meta) and not _locks.check_token(
        src_uid, request.cookies.get(_locks.cookie_name(src_uid), ""), src_meta
    ):
        # The subtitle has its own lock, and the parent video's unlock cookie
        # says nothing about it. Refuse rather than serve protected bytes
        # through the back door of a sibling's page.
        return web.Response(status=404)

    cached = media.track_dir(file_uid) / f"sc{idx}.vtt"
    if cached.is_file():
        return _serve_track_file(str(cached), "text/vtt; charset=utf-8")

    # This route converts the whole sidecar in memory, and /subs/ is not behind
    # the rate limiter, so the size has to be bounded here rather than trusted:
    # a "subtitle" uploaded as a 2 GB document would otherwise be pulled into
    # RAM on every concurrent hit until the cache file appeared. The declared
    # size comes from the bot's own ingest record, not from the client.
    if int(src_meta.get("file_size") or 0) > _MAX_SIDECAR_BYTES:
        return web.Response(status=413, content_type="text/plain",
                            text="Subtitle file too large")

    try:
        msg = await _get_cached_message(client, int(src_meta["msg_id"]))
        raw = await client.download_media(msg, in_memory=True)
        if not raw:
            return web.Response(status=404)
        text = bytes(raw.getbuffer()).decode("utf-8", "replace")
        name = src_meta.get("file_name") or ""
        # Dispatch on the actual sidecar format. This used to be "if it isn't
        # .vtt, run the SRT parser", which quietly produced an EMPTY cue list
        # for .ass/.ssa — ASS is a sectioned document, so srt_to_vtt() found
        # no timestamp blocks in it and the track rendered as valid but blank.
        kind = media.has_sidecar_subtitle(name) or "srt"
        vtt = media.sidecar_to_vtt(text, kind)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(vtt, encoding="utf-8")
    except Exception as e:
        logger.warning("Sidecar subtitle %s for %s failed: %s", src_uid,
                       file_meta.get("_uid") or "?", e)
        return web.Response(status=502, content_type="text/plain",
                            text="Subtitle conversion failed")
    return _serve_track_file(str(cached), "text/vtt; charset=utf-8",
                           cache_control="public, max-age=86400")


async def audio_track_handler(request: web.Request):
    """GET /media/<file_uid>/audio/<index> → one remuxed audio stream."""
    file_uid = request.match_info["file_uid"]
    db = request.app["db"]
    idx = _track_index(request)
    if idx is None:
        return web.Response(status=404)

    file_meta = await db.get_file(file_uid)
    if not file_meta:
        return web.Response(status=404)

    denied = _locked_response(request, file_meta)
    if denied:
        return denied

    entry = next((a for a in (file_meta.get("media") or {}).get("audio", [])
                  if a.get("index") == idx), None)
    if not entry:
        return web.Response(status=404)

    ext = (entry.get("ext") or "m4a").lower()
    path = media.track_dir(file_uid) / f"audio{idx}.{ext}"
    if not path.is_file():
        # Fall back to any extension — the manifest and disk can disagree
        # briefly if an extraction was interrupted.
        matches = sorted(media.track_dir(file_uid).glob(f"audio{idx}.*"))
        if not matches:
            return web.Response(status=404)
        path = matches[0]
        ext = path.suffix.lstrip(".").lower()

    return _serve_track_file(
        str(path), _AUDIO_CONTENT_TYPES.get(ext, "application/octet-stream")
    )


async def tracks_prepare_handler(request: web.Request):
    """
    POST /tracks/<file_uid> — extract tracks now for a file that has none.

    Exists for videos stored before FFmpeg was installed (or whose extraction
    failed/was skipped): their links are otherwise permanently track-less,
    since extraction only ran at upload time. Kicks the job off in the
    background and answers immediately — a multi-GB download plus FFmpeg run
    is far too slow to hold an HTTP request open for.
    """
    file_uid = request.match_info["file_uid"]
    db = request.app["db"]
    client = request.app["client"]

    file_meta = await db.get_file(file_uid)
    if not file_meta:
        return web.json_response({"error": "not found"}, status=404)
    denied = _locked_response(request, file_meta, api=True)
    if denied:
        return denied
    if not media.available():
        return web.json_response(
            {"error": "ffmpeg-unavailable",
             "message": "This server has no FFmpeg installed, so embedded "
                        "tracks cannot be extracted."},
            status=503,
        )
    if (file_meta.get("media") or {}).get("subtitles") or (file_meta.get("media") or {}).get("audio"):
        return web.json_response({"status": "ready"})
    if request.app.get("preparing", {}).get(file_uid):
        return web.json_response({"status": "preparing"}, status=202)

    request.app.setdefault("preparing", {})[file_uid] = True

    async def _job():
        try:
            await media.prepare_tracks(
                client, db, file_uid,
                int(file_meta["msg_id"]),
                int(file_meta.get("file_size") or 0),
                file_meta.get("file_name") or "",
            )
        finally:
            request.app.get("preparing", {}).pop(file_uid, None)

    asyncio.create_task(_job())
    return web.json_response({"status": "preparing"}, status=202)


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

    denied = _locked_response(request, file_meta, webpage=True)
    if denied:
        return denied

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

    # ── Subtitle + audio track lists ─────────────────────────────────────────
    # Two independent sources, merged into one list per kind:
    #   • `media` — written by web/media.py when FFmpeg extracted embedded
    #     streams from the video itself (the only way to get at the
    #     multi-language subs/dubs baked into an MKV).
    #   • sidecars — separate .srt/.vtt files uploaded alongside the video in
    #     the same batch, converted to WebVTT on first request.
    # Absent both, the player just offers no track controls.
    subtitle_tracks: list = []
    audio_tracks: list = []
    media_manifest = file_meta.get("media") or {}
    if is_video:
        for sub in media_manifest.get("subtitles", []):
            subtitle_tracks.append({
                "label": sub.get("label") or "Subtitle",
                "language": sub.get("language") or "",
                "default": bool(sub.get("default")),
                "src": f"/subs/{file_uid}/{sub.get('index')}",
            })
        for aud in media_manifest.get("audio", []):
            audio_tracks.append({
                "label": aud.get("label") or "Audio",
                "language": aud.get("language") or "",
                "default": bool(aud.get("default")),
                "src": f"/media/{file_uid}/audio/{aud.get('index')}",
            })
        subtitle_tracks.extend(
            await _collect_sidecar_subtitles(db, file_meta, file_name, file_uid)
        )

    # A video with no embedded tracks yet, on a server that could make them,
    # gets an explicit "load tracks" affordance rather than looking broken.
    can_prepare_tracks = bool(
        is_video and media.available()
        and not media_manifest.get("subtitles") and not media_manifest.get("audio")
    )

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
        subtitle_tracks=subtitle_tracks, audio_tracks=audio_tracks,
        can_prepare_tracks=can_prepare_tracks,
        tracks_endpoint=f"/tracks/{file_uid}",
    )
    return web.Response(text=html, content_type="text/html")


# ── Route: POST /file/<id> (locked links only) ───────────────────────────────
# A password is a short secret typed into a small form field; the app-wide
# client_max_size is 4 GB because /download-style routes must accept anything.
# request.post() on this route would honour THAT ceiling, so the body is read
# by hand with its own cap.
_MAX_FORM_BYTES = 8192


async def _form_password(request) -> "str | None":
    """Return the submitted password, "" when absent, None when unreadable."""
    if request.content_type.startswith("multipart/"):
        # A multipart body is almost always far bigger than what fits in the
        # bytes we're willing to read, so parsing the truncated head would
        # produce a confidently wrong answer. Refuse it as unreadable.
        return None
    try:
        raw = await request.content.read(_MAX_FORM_BYTES + 1)
    except Exception as e:
        logger.debug("Could not read unlock form: %s", e)
        return None
    if len(raw) > _MAX_FORM_BYTES:
        return None
    from urllib.parse import parse_qs
    try:
        fields = parse_qs(raw.decode("utf-8", "replace"), max_num_fields=8)
    except Exception:
        return None
    return (fields.get("password") or [""])[0]


async def password_post_handler(request: web.Request):
    """
    Checks the password for a locked link and, on success, sets the unlock
    cookie and hands off to the normal GET /file/<id>.

    The answer is never reflected back (only "correct"/"incorrect"), the
    attempt gate is keyed on client IP *and* file so one person's guessing
    can't lock out a shared office, and the KDF runs on a worker thread so a
    password check can't stall the streams of everyone else on this server.
    """
    file_uid = request.match_info["file_uid"]
    db = request.app["db"]

    file_meta = await db.get_file(file_uid)
    if not file_meta:
        return web.Response(
            status=404, content_type="text/html",
            text=_render(
                "error_page.html",
                title="File Not Found",
                message="This file does not exist or has been deleted.",
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
    if not _locks.is_locked(file_meta):
        # Unlocked while this form was open — the page is public again.
        return web.HTTPSeeOther(f"/file/{file_uid}")

    ip = _client_ip(request)
    wait = _locks.blocked(ip, file_uid)
    if wait:
        return _lock_page(
            request, status=429, retry_seconds=wait,
            error=(f"Too many incorrect attempts. Wait "
                   f"{wait // 60}m {wait % 60}s before trying again."
                   if wait >= 60 else f"Too many incorrect attempts. Wait {wait}s."),
        )

    password = await _form_password(request)
    if password is None:
        return _lock_page(
            request, status=400,
            error="That submission could not be read. Please try again.",
        )
    if not password.strip():
        return _lock_page(request, status=400, error="Enter the password first.")

    record = file_meta["lock"]
    if not await _locks.check_password(password, record):
        left = _locks.note_failure(ip, file_uid)
        return _lock_page(
            request,
            error=("❌ Wrong password. This link is now locked against further "
                   "attempts for a while." if left == 0 else
                   f"❌ Wrong password. {left} attempt{'s' if left != 1 else ''} "
                   "left before this link temporarily locks."),
        )

    _locks.clear_failures(ip, file_uid)
    response = web.HTTPSeeOther(f"/file/{file_uid}")
    response.set_cookie(
        _locks.cookie_name(file_uid),
        _locks.make_token(file_uid, record),
        max_age=_locks.UNLOCK_TTL_SECONDS,
        path="/",
        httponly=True,
        samesite="Lax",
        # Only over HTTPS when we're actually being served over it; a plain
        # http:// host (localhost, or a bot on a bare IP) must still work,
        # and a Secure cookie there could never be set at all.
        #
        # `client_is_https`, not `request.secure`: the latter only sees the
        # transport aiohttp accepted, so behind the usual TLS-terminating
        # reverse proxy it was False on a genuinely https site and this
        # authentication cookie shipped without its Secure flag.
        #
        # (`request.secure`, incidentally, rather than the old
        # `request.is_secure()` — aiohttp 3.13 removed that method and the
        # requirements floor is >=3.9, where calling it was a 500 on every
        # correct password.)
        secure=client_is_https(request),
    )
    return response


async def _collect_sidecar_subtitles(db, file_meta: dict, file_name: str,
                                     file_uid: str) -> list:
    """
    Find subtitle files uploaded alongside this video in the same batch.

    Browsers only parse WebVTT in a <track> element, so .srt/.ass sidecars are
    converted on demand by subtitle_handler; we just describe them here.
    Subtitle files themselves are never offered as "videos", and each one is
    excluded from being treated as its own sibling.

    Returns manifest-shaped entries (index/label/language/src_uid) using
    indices continuing after any FFmpeg tracks would have used — the caller
    merges them with those, so we reserve from a high base to avoid collision.
    """
    batch_id = file_meta.get("batch_id")
    if not batch_id:
        return []
    out = []
    try:
        batch = await db.get_batch(batch_id)
        if not batch:
            return []
        stem = os.path.splitext(file_name)[0].lower()
        # PERF: this runs on every render of a batch member's page, and used
        # to fetch each sibling one at a time to look for a subtitle sidecar —
        # so a 30-file batch cost 29 sequential DB round trips per page view,
        # almost all of them for a batch that contains no subtitles at all.
        siblings = [u for u in batch.get("files", []) if u != file_uid]
        sib_metas = await asyncio.gather(*(db.get_file(u) for u in siblings)) if siblings else []
        found = []
        for sib_uid, sib in zip(siblings, sib_metas):
            if not sib:
                continue
            # A password on the sidecar means its owner hid that file — and this
            # loop would otherwise publish a fragment of its name (the language
            # token, e.g. "EN") on the *parent's* public page, plus a link that
            # then 404s. Leave locked siblings out entirely.
            if _locks.is_locked(sib):
                continue
            # Same ceiling subtitle_handler enforces, applied here so the page
            # never emits a <track> that is guaranteed to fail.
            if int(sib.get("file_size") or 0) > _MAX_SIDECAR_BYTES:
                continue
            sib_name = sib.get("file_name") or ""
            kind = media.has_sidecar_subtitle(sib_name)
            if not kind:
                continue
            sib_stem = os.path.splitext(sib_name)[0].lower()
            # "movie.en.srt" / "movie" both belong to "movie.mkv"; a batch of
            # several unrelated videos shouldn't cross-link their subtitles.
            if sib_stem != stem and not sib_stem.startswith(stem + "."):
                continue
            label = (sib_stem[len(stem):].lstrip(".") or "Captions").upper()[:8]
            found.append((sib_uid, label, kind))

        for i, (sib_uid, label, _kind) in enumerate(found):
            lang = label.lower() if len(label) == 2 else ""
            out.append({
                "index": 100 + i,
                "label": f"{label} (uploaded file)" if label else "Captions",
                "language": lang,
                "default": i == 0,
                "src_uid": sib_uid,
                # file_page.html emits `track.src` for every subtitle track; a
                # sidecar that only carried src_uid rendered as src="" and the
                # browser silently dropped the whole <track>.
                "src": f"/subs/{file_uid}/{100 + i}",
            })
    except Exception as e:
        logger.debug("Subtitle sidecar lookup failed: %s", e)
        return []
    return out


# ── Card decoration (batch grid + latest listing) ─────────────────────────────
def _decorate_card(file_uid: str, fm: dict, base_url: str) -> dict:
    """Add the render-time fields every file card needs, in one place.

    Shared by the batch grid and /latest because both draw the same card, and
    the streamability half of this used to be duplicated wrong: the batch grid
    decided it from the Telegram message type alone while the file page also
    checked the MIME/extension, so an .mkv that arrived as a generic
    "document" offered a Stream button on its own page and not in the batch —
    same file, two answers.
    """
    fm["_uid"]          = file_uid
    fm["_download_url"] = f"{base_url}/download/{file_uid}"
    fm["_stream_url"]   = f"{base_url}/stream/{file_uid}"
    fm["_page_url"]     = f"{base_url}/file/{file_uid}"
    fm["_thumb_url"]    = (f"{base_url}/thumbnail/{file_uid}"
                           if fm.get("type") in ("video", "photo", "animation") else None)
    fm["_size_human"]   = humanbytes(int(fm.get("file_size") or 0))
    fm["_icon"]         = ICON_MAP.get(fm.get("type", "document"), "📁")
    fm["_streamable"]   = is_streamable_media(
        fm.get("type", ""), mime_type=fm.get("mime_type"),
        file_name=fm.get("file_name"),
    )
    return fm


def _locked_card(file_uid: str, base_url: str) -> dict:
    """
    A placeholder card for a locked file in a batch grid.

    The alternative — dropping it from the list — made a locked file vanish
    without trace, so the uploader who was *given* the batch link thought the
    transfer had failed. This keeps the count honest and the "why is this one
    different" obvious, while naming nothing: no filename, no size, no type,
    no thumbnail.
    """
    return {
        "_locked": True,
        "_uid": file_uid,
        "_page_url": f"{base_url}/file/{file_uid}",
        "_download_url": "",
        "_stream_url": "",
        "_streamable": False,
        "_thumb_url": None,
        "_size_human": "",
        "_icon": "🔒",
        "file_name": "Password protected",
        "type": "locked",
    }


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

    # PERF: this used to be a serial `for` loop of `await db.get_file()` —
    # one network round trip per file before the page could render, so a
    # 20-file batch paid the latency twenty times over. Gathered instead, so
    # the whole grid costs roughly one round trip. gather preserves input
    # order, so the batch's own file ordering is unchanged.
    uids = list(batch.get("files", []))
    metas = await asyncio.gather(*(db.get_file(u) for u in uids)) if uids else []

    files_meta = []
    for fuid, fm in zip(uids, metas):
        # BUG FIX: an expired member used to keep its card, with a Download
        # and Stream button that failed the moment they were clicked. The
        # file page already refuses such links, and delete_expired_files()
        # drops these records altogether on the hourly sweep — so skipping
        # them here just makes that happen at render time instead of leaving
        # up to an hour of dead buttons on the page.
        if not fm or _is_expired(fm):
            continue
        if _locks.is_locked(fm):
            files_meta.append(_locked_card(fuid, base_url))
            continue
        files_meta.append(_decorate_card(fuid, fm, base_url))

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
    denied = _locked_response(request, file_meta, api=True)
    if denied:
        return denied
    # `lock` is stripped as well: the salt + PBKDF2 digest are exactly what an
    # offline guesser needs, and /info is a public endpoint on an unguessable
    # path — handing them out would turn "guess the uid AND crack the hash"
    # into "just crack the hash, at whatever rate your GPU likes".
    safe = {k: v for k, v in file_meta.items()
            if k not in ("_id", "file_id", "lock")}
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
    gallery = cfg.public_gallery()
    html = _render(
        "index.html",
        canonical_url=(base_url + "/") if base_url else None,
        # When the gallery is off this stays [] — the landing page doesn't
        # even touch the database for it.
        recent=await _recent_cards(request, _STRIP_LIMIT) if gallery else [],
    )
    return web.Response(text=html, content_type="text/html")


# ── Route: /latest (opt-in public gallery) ────────────────────────────────────
#
# Off unless the admin turns on `public_gallery` (see settings_store). This is
# the one feature in this bot that works against its core premise: every link
# here is private by being unguessable, and a listing of recent uploads turns
# that into a browsable index — including filenames people uploaded for one
# specific recipient. So the default is closed, the page is noindex, and
# robots.txt keeps crawlers out of it whether or not it's enabled.
_LATEST_LIMIT = 24       # cards on /latest itself
_STRIP_LIMIT = 8         # cards on the landing page
_LATEST_TTL = 30.0       # seconds
_latest_cache: tuple = (0.0, [])


async def _recent_cards(request: web.Request, count: int):
    """Newest non-expired uploads, decorated as cards.

    Cached for _LATEST_TTL seconds because this now runs on the landing page,
    which the rate limiter deliberately doesn't limit (a real visitor's first
    hit fires a few in parallel). Without the cache, an unauthenticated
    flood of "/" requests would each cost a database query; with it, the whole
    burst costs one. Being up to 30s stale is invisible on a "recent" list —
    a card for something just deleted with /unlink can linger that long and
    then 404 on click, which is the correct answer either way.
    """
    db = request.app["db"]
    base_url = request.app.get("base_url", "")
    now = time.monotonic()
    global _latest_cache
    stamp, cards = _latest_cache
    if now - stamp >= _LATEST_TTL:
        # One indexed query for the page-sized list; the smaller landing-page
        # strip is a slice of the same result rather than a second query.
        fresh = []
        for f in await db.get_recent_files(_LATEST_LIMIT):
            uid = f.get("_id") or f.get("file_uid")
            # A locked file is dropped rather than shown as a placeholder
            # here: /latest is a public index, and even "there is a protected
            # file named nothing, uploaded Tuesday" is a fact the uploader
            # chose to hide.
            if not uid or _is_expired(f) or _locks.is_locked(f):
                continue
            fresh.append(_decorate_card(uid, f, base_url))
        _latest_cache = (now, fresh)
        cards = fresh
    return cards[:count]


async def latest_page_handler(request: web.Request):
    if not cfg.public_gallery():
        return web.Response(
            status=404, content_type="text/html",
            text=_render(
                "error_page.html",
                title="Not Found",
                message="This page does not exist.",
                code=404,
            ),
        )
    base_url = request.app.get("base_url", "")
    cards = await _recent_cards(request, _LATEST_LIMIT)
    html = _render(
        "latest_page.html",
        files=cards,
        page_title=f"Latest files — {SITE_NAME}",
        page_desc=f"The {len(cards)} most recent links shared through {SITE_NAME}.",
        robots="noindex, follow",
        canonical_url=None,
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
        "Disallow: /latest",
    ]
    if base_url:
        lines.append(f"Sitemap: {base_url}/sitemap.xml")
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


# ── Route: /sitemap.xml ──────────────────────────────────────────────────────
async def sitemap_handler(request: web.Request):
    """Minimal sitemap. Individual file links are private/unguessable and are
    intentionally excluded — only the public landing page is listed. /latest
    is excluded too even when enabled: it's a constantly-rotating listing of
    other people's filenames, which is worth showing to a visitor who asks
    and not worth publishing to the world's search indexes."""
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

    # Warm the link-unlock signing secret at startup rather than on the first
    # locked request: the first time it's needed it's generated and written to
    # bot_settings.json, and a synchronous file write has no business
    # happening inside a request handler (same class of stall as the
    # local-DB save fixed in plugins/).
    _locks.signing_secret()

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
    # Password form target for a locked link. The GET route above answers with
    # the prompt itself; this is where the answer goes.
    app.router.add_post("/file/{file_uid}",    password_post_handler)
    app.router.add_get("/thumbnail/{file_uid}",thumbnail_handler)
    app.router.add_get("/batch/{batch_id}",    batch_page_handler)
    app.router.add_get("/info/{file_uid}",     info_handler)
    app.router.add_get("/latest",              latest_page_handler)
    # Extracted media tracks (served from local disk, not Telegram)
    app.router.add_get("/subs/{file_uid}/{index}",  subtitle_handler)
    app.router.add_get("/media/{file_uid}/audio/{index}", audio_track_handler)
    app.router.add_post("/tracks/{file_uid}",  tracks_prepare_handler)

    # Static assets (CSS/JS/favicon) with long-lived browser caching.
    _static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(_static_dir):
        app.router.add_static("/static/", _static_dir, append_version=True)

    # Idle housekeeping (see _idle_housekeeping_loop above): started/stopped
    # via aiohttp's own app lifecycle signals rather than threading a task
    # reference through bot.py's shutdown sequence — on_cleanup already
    # fires exactly once, at the right time, whenever runner.cleanup() runs.
    async def _start_housekeeping(app):
        app["housekeeping_task"] = asyncio.create_task(_idle_housekeeping_loop(app["db"]))

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
