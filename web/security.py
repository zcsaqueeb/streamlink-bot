"""
web/security.py — response-header hardening and per-IP rate limiting for
the web layer.

Extracted from web/app.py purely for organization — behavior is unchanged,
this is a verbatim move. Depends on web/render.py (not web/app.py) for the
error-page renderer used by the 429 response, specifically to avoid a
circular import: app.py registers these middlewares, so this module can't
import anything back from app.py.
"""

import time
from collections import deque

from aiohttp import web

from web.render import render

# ── Response headers ─────────────────────────────────────────────────────────
# Adds defence-in-depth headers to every response. The CSP is deliberately
# permissive enough for our inline page scripts/styles and Google Fonts, while
# blocking framing (clickjacking), MIME sniffing and referrer leakage.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)

# Endpoints whose responses must NOT be wrapped/mutated (raw byte streams).
_SKIP_SECURITY_PREFIXES = ("/stream/", "/download/", "/thumbnail/")


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    response = await handler(request)
    # Never touch streaming/byte responses — only HTML/JSON/asset responses.
    if not request.path.startswith(_SKIP_SECURITY_PREFIXES):
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy",
                     "geolocation=(), microphone=(), camera=(), interest-cohort=()")
        h.setdefault("Content-Security-Policy", _CSP)
        # HSTS only matters over HTTPS; harmless to send and ignored on http.
        h.setdefault("Strict-Transport-Security",
                     "max-age=31536000; includeSubDomains")
    return response


# ── Per-IP rate limiter ──────────────────────────────────────────────────────
# Protects the *cheap* HTML/JSON endpoints (page, info, index) from abuse.
# IMPORTANT: streaming/download endpoints are intentionally EXEMPT — a single
# real download legitimately opens dozens of parallel range connections, so
# rate-limiting them would break the core feature. Sliding-window counter.
_RL_WINDOW = 60          # seconds
_RL_MAX    = 120         # requests per IP per window for rate-limited routes
_RL_BUCKETS: dict = {}
_RL_LIMITED_PREFIXES = ("/file/", "/batch/", "/info/")


def _client_ip(request: web.Request) -> str:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return peer[0] if peer else "unknown"


@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    if request.path.startswith(_RL_LIMITED_PREFIXES):
        ip = _client_ip(request)
        now = time.monotonic()
        dq = _RL_BUCKETS.setdefault(ip, deque())
        while dq and (now - dq[0]) > _RL_WINDOW:
            dq.popleft()
        if len(dq) >= _RL_MAX:
            return web.Response(
                status=429, content_type="text/html",
                headers={"Retry-After": str(_RL_WINDOW)},
                text=render(
                    "error_page.html",
                    title="Too Many Requests",
                    message="You've made a lot of requests in a short time. "
                            "Please wait a minute and try again.",
                    code=429,
                ),
            )
        dq.append(now)
        # Opportunistic cleanup to bound memory.
        if len(_RL_BUCKETS) > 10000:
            for k in list(_RL_BUCKETS.keys()):
                if not _RL_BUCKETS[k]:
                    _RL_BUCKETS.pop(k, None)
    return await handler(request)


# ── Idle housekeeping (rate-limit half) ──────────────────────────────────────
# See web/cache.py's sweep_message_cache() for the other half, and
# web/app.py's _idle_housekeeping_loop() for the orchestration that calls
# both on a timer.
def sweep_rate_limit_buckets() -> int:
    """Proactively drop rate-limit buckets for IPs not seen within the
    window. Returns the number of buckets dropped, purely for logging.

    A bucket is safe to drop once its NEWEST entry (dq[-1], the last
    timestamp appended) is older than the rate-limit window — rate_limit_
    middleware always re-appends the current timestamp right after trimming
    expired entries, so a touched bucket is never actually EMPTY; checking
    `not dq` alone would almost never fire. If even the most recent hit is
    stale, every entry in the bucket is, and it's safe to drop entirely (a
    future request from that IP just gets a fresh deque via setdefault(),
    no correctness loss).
    """
    now = time.monotonic()
    stale_ips = [
        ip for ip, dq in _RL_BUCKETS.items()
        if not dq or (now - dq[-1]) > _RL_WINDOW
    ]
    for ip in stale_ips:
        _RL_BUCKETS.pop(ip, None)
    return len(stale_ips)
