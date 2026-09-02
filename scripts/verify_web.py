#!/usr/bin/env python3
"""
verify_web.py — self-contained smoke test for the File-to-Link web layer.

Proves every web fix/upgrade works WITHOUT needing Telegram, MongoDB, or any
network. It stubs the `info` and `utils` modules and a fake DB, spins up the
real aiohttp app in-process, and asserts the behavior of every route.

Usage (from the repo root):
    pip install aiohttp jinja2
    python scripts/verify_web.py

Exit code 0 = all checks passed, 1 = a check failed.
"""
import asyncio
import os
import sys
import types

# This script lives in scripts/, one level below the repo root where
# web/app.py actually is — resolve HERE to the repo root, not this file's
# own directory, so the path.join(HERE, "web", "app.py") below still finds it
# no matter what directory this is invoked from.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install_stubs():
    """Stub the heavy app deps so web/app.py imports cleanly offline."""
    info = types.ModuleType("info")
    info.DB_CHANNEL = -100
    info.WEB_SERVER_BIND_ADDRESS = "0.0.0.0"
    info.WEB_SERVER_PORT = 8080
    info.SITE_NAME = "StreamLink"
    info.SITE_TAGLINE = "Generate instant links & stream anything."
    info.CREATOR_NAME = "Saqueeb"
    info.BOT_USERNAME = "test_bot"
    sys.modules["info"] = info

    utils = types.ModuleType("utils")

    def humanbytes(n):
        n = float(n)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    utils.humanbytes = humanbytes

    # NOTE: web/app.py's real import surface from utils has grown over time
    # (this stub broke silently once before, when BOT_USERNAME and these
    # four names were added upstream but never mirrored here — see git
    # history / ANALYSIS notes). If `from utils import (...)` in web/app.py
    # ever changes, this stub needs the same update or this test will fail
    # with an ImportError pointing at the missing name, which is at least
    # loud rather than silently testing stale behavior.
    import mimetypes as _mimetypes
    import os as _os

    utils.STREAMABLE_MSG_TYPES = {"video", "audio", "voice", "video_note", "animation"}
    utils.VIDEO_MIMES = {"video/mp4", "video/webm", "video/x-matroska", "video/quicktime"}
    utils.AUDIO_MIMES = {"audio/mpeg", "audio/ogg", "audio/flac", "audio/mp4"}
    _EXT_MIME_MAP = {".mkv": "video/x-matroska", ".mov": "video/quicktime", ".webm": "video/webm"}

    def detect_mime(file_name, fallback="application/octet-stream"):
        if not file_name:
            return fallback
        ext = _os.path.splitext(file_name)[1].lower()
        if ext in _EXT_MIME_MAP:
            return _EXT_MIME_MAP[ext]
        guessed, _ = _mimetypes.guess_type(file_name)
        return guessed or fallback

    def is_streamable_media(ftype, mime_type=None, file_name=None):
        if ftype in utils.STREAMABLE_MSG_TYPES:
            return True
        mime = mime_type or (detect_mime(file_name) if file_name else None)
        return bool(mime) and mime in (utils.VIDEO_MIMES | utils.AUDIO_MIMES)

    utils.detect_mime = detect_mime
    utils.is_streamable_media = is_streamable_media
    sys.modules["utils"] = utils


def _load_app_module():
    import importlib.util
    # transfer_stats is a real, dependency-free module (pure in-memory
    # counters, no I/O) — rather than stubbing it like info/utils, we let
    # web/app.py's `import transfer_stats` resolve to the actual file, so
    # HERE (the repo root) needs to be importable.
    sys.path.insert(0, HERE)
    sys.path.insert(0, os.path.join(HERE, "web"))
    spec = importlib.util.spec_from_file_location(
        "webapp", os.path.join(HERE, "web", "app.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeDB:
    async def get_file(self, uid):
        if uid == "missing":
            return None
        return {
            "msg_id": 5,
            "file_size": 1234567,
            "file_name": "Demo Movie [HD].mkv",
            "mime_type": "video/x-matroska",
            "type": "video",
            "saved_at": "2026-06-01T00:00:00",
        }

    async def get_batch(self, bid):
        return {"status": "done", "files": ["f1", "f2"]}

    async def increment_stat(self, key):
        pass

    async def increment_file_stat(self, file_id, key, amount=1):
        pass


PASSED = 0
FAILED = 0


def check(label, cond):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  \u2705 {label}")
    else:
        FAILED += 1
        print(f"  \u274c {label}")


async def run():
    _install_stubs()
    webapp = _load_app_module()

    from aiohttp.test_utils import TestClient, TestServer

    app = webapp.create_app(client=object(), db=FakeDB(), base_url="https://example.test")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        print("\nRoutes & security headers:")
        for path, expect in [
            ("/", 200), ("/file/abc", 200), ("/batch/B1", 200),
            ("/info/abc", 200), ("/robots.txt", 200), ("/sitemap.xml", 200),
            ("/static/theme.css", 200), ("/static/theme.js", 200),
            ("/static/favicon.svg", 200), ("/file/missing", 404),
        ]:
            r = await client.get(path)
            check(f"GET {path} -> {r.status} (want {expect})", r.status == expect)

        print("\nKey fixes:")
        r = await client.get("/info/abc")
        body = await r.text()
        check("/info/abc returns 200 JSON (was 500)",
              r.status == 200 and r.headers.get("Content-Type", "").startswith("application/json"))

        r = await client.get("/")
        check("Security headers present (CSP + X-Frame-Options)",
              "Content-Security-Policy" in r.headers and r.headers.get("X-Frame-Options") == "DENY")

        r = await client.head("/download/abc")
        check("HEAD /download -> 200 with Accept-Ranges",
              r.status == 200 and r.headers.get("Accept-Ranges") == "bytes")
        check("Streaming response NOT mutated with CSP (raw bytes safe)",
              "Content-Security-Policy" not in r.headers)

        print("\nRate limiter (/info, 120/min):")
        statuses = []
        for _ in range(130):
            rr = await client.get("/info/abc")
            statuses.append(rr.status)
        check("Returns 429 after the limit", 429 in statuses)
        check("Allows up to ~120 before limiting", statuses.count(200) >= 100)
    finally:
        await client.close()

    print(f"\n{'='*48}\n  PASSED: {PASSED}   FAILED: {FAILED}\n{'='*48}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except ModuleNotFoundError as e:
        print(f"Missing dependency: {e}. Run: pip install aiohttp jinja2")
        sys.exit(2)
