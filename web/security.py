"""
web/security.py — response-header hardening and per-IP rate limiting for
the web layer.

Extracted from web/app.py purely for organization — behavior is unchanged,
this is a verbatim move. Depends on web/render.py (not web/app.py) for the
error-page renderer used by the 429 response, specifically to avoid a
circular import: app.py registers these middlewares, so this module can't
import anything back from app.py.
"""

import ipaddress
import logging
import os
import time
from collections import deque

from aiohttp import web

from web.render import render

logger = logging.getLogger(__name__)

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
# /tracks/ is the on-demand FFmpeg extraction trigger — a POST that queues
# real CPU/disk work, so it belongs with the limited set rather than being
# exempt just because it isn't an HTML page.
_RL_LIMITED_PREFIXES = ("/file/", "/batch/", "/info/", "/tracks/")


def _peer_ip(request: web.Request) -> str:
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return peer[0] if peer else "unknown"


def _peer_is_proxy(peer: str) -> bool:
    """Is the socket peer allowed to have appended a forwarding header?

    SECURITY FIX: trusting X-Forwarded-For whenever it merely *exists* was the
    old behaviour, and it's forgeable — a client that reaches this server
    directly (no proxy at all: the plain `python bot.py` / Railway-style
    bind) can send a fresh random value per request and get a brand-new
    rate-limit bucket every time, which also resets the /lock attempt counter.
    That turns the password gate into a CPU amplifier: an unlimited number of
    ~120k-iteration PBKDF2 runs per second.

    The header is only meaningful when it came from a proxy, so we now require
    the peer itself to be one: loopback or a private/link-local range covers
    nginx/Caddy on the host and container-network ingress (Railway, Render,
    Fly), which is nearly every deployment. Anything else is ignored unless the
    operator names it explicitly in TRUSTED_PROXY_CIDRS.
    """
    if not peer or peer == "unknown":
        return False
    configured = os.environ.get("TRUSTED_PROXY_CIDRS", "").strip()
    if configured:
        try:
            net = ipaddress.ip_address(peer)
            for entry in configured.split(","):
                entry = entry.strip()
                if entry and net in ipaddress.ip_network(entry, strict=False):
                    return True
        except ValueError:
            logger.warning("TRUSTED_PROXY_CIDRS has an unreadable entry; ignoring it.")
        return False
    try:
        net = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return net.is_loopback or net.is_private or net.is_link_local


def _forwarded_hops(request: web.Request, header: str) -> list:
    """Forwarding-chain values, but only when the peer is a real proxy."""
    if not _peer_is_proxy(_peer_ip(request)):
        return []
    raw = request.headers.get(header)
    if not raw:
        return []
    return [h.strip() for h in raw.split(",") if h.strip()]


def _trusted_count(available: int) -> int:
    try:
        trusted = int(os.environ.get("TRUSTED_PROXY_COUNT", "1") or 1)
    except ValueError:
        trusted = 1
    return max(1, min(trusted, available))


def _client_ip(request: web.Request) -> str:
    """Best-effort client IP for rate-limit and lockout bucketing.

    Uses the RIGHTMOST X-Forwarded-For entry — the hop appended by the closest
    proxy, which a client cannot rewrite — and only when that proxy is the
    socket peer. Otherwise the peer address is the answer.
    """
    hops = _forwarded_hops(request, "X-Forwarded-For")
    if hops:
        return hops[-_trusted_count(len(hops))]
    return _peer_ip(request)


def client_is_https(request: web.Request) -> bool:
    """Whether the client's own connection was TLS, proxy header included.

    `request.secure` only ever reflects the transport aiohttp accepted, which
    is plain HTTP for the common TLS-terminating reverse proxy. That made the
    /lock unlock cookie issue without its Secure flag on an https site, so it
    could cross the wire in cleartext on the internal hop and be replayed by
    anyone watching it. Same trust rule as _client_ip: the header counts only
    when it came from a proxy we believe.
    """
    # `.secure`, not is_secure(): aiohttp 3.13 removed the method, and
    # requirements.txt floors at >=3.9, where this spelling is already correct.
    if request.secure:
        return True
    hops = _forwarded_hops(request, "X-Forwarded-Proto")
    return bool(hops) and hops[-_trusted_count(len(hops))].lower() == "https"


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
        # Opportunistic cleanup to bound memory. A touched bucket is never
        # literally empty (we just appended to it), so the old `if not bucket`
        # test could never fire and this loop was dead — a burst from many
        # distinct IPs could still grow _RL_BUCKETS to its cap and stay there
        # until the idle sweep. Drop buckets whose NEWEST hit is already
        # outside the window; they carry no rate-limiting information left.
        if len(_RL_BUCKETS) > 10000:
            for k, v in list(_RL_BUCKETS.items()):
                if not v or (now - v[-1]) > _RL_WINDOW:
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
