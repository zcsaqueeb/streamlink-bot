"""
web/cache.py — short-lived caches that sit between the web server and
Telegram, plus fatal-vs-transient Telegram error detection.

Extracted from web/app.py (previously all top-level module state/functions
in that one file) into its own module purely for organization — behavior
is unchanged, this is a verbatim move.
"""

import logging
import time

from info import DB_CHANNEL

logger = logging.getLogger(__name__)

# FEATURE #4 — real "python-magic fallback to mimetypes" implementation.
# Extension-based guessing (mimetypes / our own table) covers the vast
# majority of files, but breaks for files with a missing, wrong, or generic
# (.bin/.dat) extension. When that happens we sniff the FIRST chunk of actual
# file bytes with libmagic. This is optional — if python-magic / libmagic
# isn't installed on the host, we silently skip sniffing and keep using the
# extension-based guess, so nothing breaks on systems without it.
try:
    import magic as _magic_module
    _magic_detector = _magic_module.Magic(mime=True)
    MAGIC_AVAILABLE = True
except Exception:
    _magic_detector = None
    MAGIC_AVAILABLE = False


# ── Short-lived message cache ────────────────────────────────────────────────
# Avoids calling client.get_messages() on EVERY parallel range request.
# Download managers open dozens of connections for the same file at once —
# this cache makes all of them hit a single Telegram API round-trip.
_MSG_CACHE: dict = {}
_MSG_CACHE_TTL = 30 * 60   # 30 minutes
_MSG_CACHE_MAX = 1000


# ── Fatal vs transient Telegram errors ───────────────────────────────────────
# BUG FIX (AUTH_BYTES_INVALID retry storm): the old attempt-loop treated every
# stream_media() failure the same way — log a warning and silently retry from
# offset 0. For a genuinely transient hiccup (a dropped connection, a slow DC)
# that's the right call. But AUTH_BYTES_INVALID / AUTH_KEY_* errors mean the
# Telegram session itself is broken; retrying the SAME broken session just
# reproduces the identical failure every time; it can never succeed until the
# process restarts with a fresh session. Retrying those anyway is what caused
# the flood of repeated "AUTH_BYTES_INVALID" tracebacks and "Connection lost"
# warnings for the same file, over and over. Detect them up front so we can
# fail fast with a clear message instead of hammering Telegram and the logs.
_FATAL_AUTH_ERRORS = (
    "AUTH_BYTES_INVALID",
    "AUTH_KEY_INVALID",
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_PERM_EMPTY",
    "SESSION_REVOKED",
    "SESSION_EXPIRED",
)


def is_fatal_auth_error(exc: BaseException) -> bool:
    return any(code in str(exc) for code in _FATAL_AUTH_ERRORS)


async def get_cached_message(client, msg_id: int, force_refresh: bool = False):
    """Fetch a message with caching to avoid Telegram API spam."""
    now = time.monotonic()
    cached = _MSG_CACHE.get(msg_id)
    if not force_refresh and cached and (now - cached[1]) < _MSG_CACHE_TTL:
        return cached[0]

    message = await client.get_messages(DB_CHANNEL, msg_id)
    _MSG_CACHE[msg_id] = (message, now)

    # Evict oldest 20% when cache is full
    if len(_MSG_CACHE) > _MSG_CACHE_MAX:
        oldest = sorted(_MSG_CACHE.items(), key=lambda kv: kv[1][1])
        for k, _ in oldest[:_MSG_CACHE_MAX // 5]:
            _MSG_CACHE.pop(k, None)

    return message


# ── Content-sniffing cache (python-magic fallback) ───────────────────────────
# Keyed by file_uid so we only ever sniff a given file once, no matter how
# many parallel range requests come in for it.
_SNIFF_CACHE: dict = {}
_SNIFF_CACHE_MAX = 1000


async def sniff_mime(client, message, file_uid: str) -> "str | None":
    """
    Read the first Pyrogram chunk of `message`'s media and identify its real
    MIME type from magic bytes. Used only when extension-based guessing was
    inconclusive (generic/missing extension). Returns None if python-magic
    isn't installed, the message has no media, or sniffing fails for any
    reason — callers always keep their extension-based guess as a fallback.
    """
    if not MAGIC_AVAILABLE:
        return None

    cached = _SNIFF_CACHE.get(file_uid)
    if cached is not None:
        return cached or None

    sniffed = None
    try:
        async for chunk in client.stream_media(message, limit=1):
            data = bytes(chunk)
            if data:
                sniffed = _magic_detector.from_buffer(data[:4096])
            break
    except Exception as e:
        logger.debug("magic sniff failed for %s: %s", file_uid, e)

    # Cache both hits and misses ("" means "tried, nothing useful") so a
    # broken/unreadable file doesn't get re-sniffed on every request.
    _SNIFF_CACHE[file_uid] = sniffed or ""
    if len(_SNIFF_CACHE) > _SNIFF_CACHE_MAX:
        for k in list(_SNIFF_CACHE.keys())[: _SNIFF_CACHE_MAX // 5]:
            _SNIFF_CACHE.pop(k, None)

    return sniffed


def peek_sniffed_mime(file_uid: str) -> "str | None":
    """
    Non-async lookup of a PREVIOUSLY sniffed MIME type, without triggering a
    new sniff. Used by file_page_handler to reuse a result a prior /stream
    or /download hit already discovered (via sniff_mime above), instead of
    every caller needing to know _SNIFF_CACHE's internal dict shape.
    """
    return _SNIFF_CACHE.get(file_uid) or None


def clear_entry(msg_id, file_uid: str) -> None:
    """
    Drop both caches' entries for one file — called by stream_handler once
    a download/stream has been served all the way through to the end of
    the file (see web/app.py). This only frees in-memory performance
    caches on this server process; it never touches the actual file,
    database record, or shareable link. Exposed as a function (rather than
    callers popping _MSG_CACHE/_SNIFF_CACHE directly) so this module keeps
    full ownership of its own internal representation.
    """
    _MSG_CACHE.pop(msg_id, None)
    _SNIFF_CACHE.pop(file_uid, None)


# ── Idle housekeeping (message-cache half) ───────────────────────────────────
# See web/security.py's sweep_rate_limit_buckets() for the other half, and
# web/app.py's _idle_housekeeping_loop() for the orchestration that calls
# both on a timer. Split this way because the two caches being swept here
# are genuinely different concerns (Telegram message caching vs. per-IP
# rate-limit bookkeeping) that happen to both benefit from the same "shrink
# proactively while idle instead of only on next write" treatment.
def sweep_message_cache() -> int:
    """Proactively drop TTL-expired message cache entries. Returns the
    number of entries dropped, purely for logging by the caller."""
    now = time.monotonic()
    expired = [k for k, (_, ts) in _MSG_CACHE.items() if (now - ts) >= _MSG_CACHE_TTL]
    for k in expired:
        _MSG_CACHE.pop(k, None)
    return len(expired)
