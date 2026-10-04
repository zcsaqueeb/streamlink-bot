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
import tempfile
import types

# This script lives in scripts/, one level below the repo root where
# web/app.py actually is — resolve HERE to the repo root, not this file's
# own directory, so the path.join(HERE, "web", "app.py") below still finds it
# no matter what directory this is invoked from.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# web/locks.py persists the cookie-signing secret through settings_store,
# which reads SETTINGS_PATH at import time. Point it at a throwaway file: the
# lock tests below make the first request that needs a secret, and without
# this the suite would create (or worse, rewrite) a real bot_settings.json in
# the repo root as a side effect of passing.
os.environ["SETTINGS_PATH"] = os.path.join(
    tempfile.gettempdir(), "verify_web_settings.json")


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
    # public_gallery() is read live on every request (info.py's module
    # __getattr__), so web/app.py can't have snapshotted it — the stub has to
    # be callable and flippable mid-run, which is what the gallery tests below
    # do through this dict.
    GALLERY = {"on": False}
    info.public_gallery = lambda: GALLERY["on"]
    info._gallery = GALLERY
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

    # Mirrors utils.py's expiry helpers (see the note above): web/app.py calls
    # these on every file/stream/info request, so they must exist here too.
    from datetime import datetime as _datetime, timezone as _timezone

    def expiry_datetime(file_meta):
        value = (file_meta or {}).get("expires_at")
        if not value:
            return None
        if isinstance(value, str):
            try:
                value = _datetime.fromisoformat(value)
            except ValueError:
                return None
        elif not isinstance(value, _datetime):
            return None
        if value.tzinfo is not None:
            value = value.astimezone(_timezone.utc).replace(tzinfo=None)
        return value

    def is_expired(file_meta):
        value = expiry_datetime(file_meta)
        return value is not None and _datetime.utcnow() > value

    utils.expiry_datetime = expiry_datetime
    utils.is_expired = is_expired

    # Password helpers for web/locks.py, mirroring utils.py's real ones with
    # one deliberate difference: 1,000 iterations instead of the production
    # 120,000. The KDF is what makes a guess expensive, and the suite makes
    # dozens of guesses — with the real cost it would spend most of its run
    # inside hashlib. The shape of the record (kdf name, salt, digest,
    # fingerprint) is identical, which is all the web layer looks at.
    import base64 as _b64
    import hashlib as _hashlib
    import hmac as _hmac

    _KDF = "pbkdf2_hmac_sha256"
    _ITERATIONS = 1_000

    def hash_password(password):
        salt = os.urandom(16)
        return {
            "kdf": _KDF,
            "iterations": _ITERATIONS,
            "salt": _b64.b64encode(salt).decode(),
            "hash": _b64.b64encode(
                _hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     salt, _ITERATIONS)).decode(),
        }

    def verify_password(password, record):
        if not record or not password or record.get("kdf") != _KDF:
            return False
        try:
            salt = _b64.b64decode(record["salt"])
            want = _b64.b64decode(record["hash"])
            iters = int(record.get("iterations") or _ITERATIONS)
        except (KeyError, TypeError, ValueError):
            return False
        return _hmac.compare_digest(
            _hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters),
            want)

    def lock_fingerprint(record):
        if not record:
            return ""
        return _hashlib.sha256(
            f"{record.get('kdf')}|{record.get('iterations')}|{record.get('salt')}"
            .encode()).hexdigest()[:16]

    utils.hash_password = hash_password
    utils.verify_password = verify_password
    utils.lock_fingerprint = lock_fingerprint
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
    # uid → the password its lock was hashed from. Two files so a cookie
    # issued for one can be tried against the other.
    PASSWORDS = {"locked": "hunter2", "locked2": "swordfish",
                 "locksrc": "sesame3", "subjp": "hidden",
                 "locked3": "pw3", "locked4": "pw4"}

    def __init__(self):
        # Counted so the gallery tests can prove the 30s cache is actually
        # being used instead of re-querying on every hit.
        self.recent_calls = 0
        # Live lock records rather than constants: re-locking a file with a
        # fresh password mid-suite is how the cookie-invalidation test gets
        # written down instead of asserted in a docstring.
        utils = sys.modules["utils"]
        self.locks = {uid: utils.hash_password(pw)
                      for uid, pw in self.PASSWORDS.items()}

    async def get_file(self, uid):
        if uid == "missing":
            return None
        meta = {
            "msg_id": 5,
            "file_size": 1234567,
            "file_name": "Demo Movie [HD].mkv",
            "mime_type": "video/x-matroska",
            "type": "video",
            "saved_at": "2026-06-01T00:00:00",
        }
        if uid == "tracks":
            # What web/media.py writes once FFmpeg has probed a multi-track
            # video — lets the page/track routes be checked without FFmpeg.
            meta["media"] = {
                "subtitles": [
                    {"index": 0, "codec": "subrip", "language": "en",
                     "label": "English", "default": True, "supported": True},
                    {"index": 1, "codec": "ass", "language": "hi",
                     "label": "Hindi", "default": False, "supported": True},
                ],
                "audio": [
                    {"index": 2, "codec": "aac", "ext": "m4a", "mode": "copy",
                     "language": "hi", "label": "Hindi", "supported": True},
                ],
                "duration": 1200.0,
            }
        if uid in self.locks:
            # The protected sidecar has to keep a sidecar-looking name, or the
            # locked-sibling check below would pass for the wrong reason — an
            # unrecognised extension rather than the lock itself.
            meta["file_name"] = ("movie.jp.srt" if uid == "subjp"
                                 else f"Private {uid.upper()}.mkv")
            meta["lock"] = self.locks[uid]
        if uid == "subparent":
            # A video whose attached subtitle file has a lock of its own. The
            # video is public; the subtitle isn't, so /subs/<this>/200 has to
            # refuse instead of serving protected bytes through the back door
            # of a sibling's page. "locksrc" is never unlocked by this suite,
            # unlike "locked", whose cookie the client does collect.
            meta["media"] = {"subtitles": [
                {"index": 200, "label": "Secret", "language": "en",
                 "default": True, "src_uid": "locksrc"},
            ]}
        if uid == "movie":
            # A batch video with two sidecar subtitles of its own: one public,
            # one password-protected. Exercises the discovery path that turns
            # an uploaded .srt into a <track>, which no other uid here reaches
            # (the manifest-based ones never call _collect_sidecar_subtitles).
            meta["file_name"] = "movie.mkv"
            meta["batch_id"] = "bsub"
        if uid in ("suben", "subjp"):
            meta["file_name"] = f"movie.{'en' if uid == 'suben' else 'jp'}.srt"
            meta["batch_id"] = "bsub"
            meta["type"] = "document"
            meta["mime_type"] = "application/x-subrip"
        if uid == "bigvid":
            meta["file_name"] = "big.mkv"
            meta["batch_id"] = "bbig"
            # A /sub-style attached subtitle in the 200+ band reaches the
            # converter through the media manifest rather than the batch
            # sibling scan, so it sidesteps the collector's own filter — this
            # is the route the handler-side ceiling actually exists for.
            meta["media"] = {"subtitles": [
                {"index": 200, "label": "Huge", "language": "",
                 "default": True, "src_uid": "bigsub"},
            ]}
        if uid == "bigsub":
            # A 500 MB "subtitle": /subs/ converts sidecars in memory, so the
            # route has to refuse this on the declared size instead of
            # downloading it — the request is not rate-limited, and one of
            # these per page view would be enough to exhaust a small host.
            meta["file_name"] = "big.srt"
            meta["batch_id"] = "bbig"
            meta["type"] = "document"
            meta["file_size"] = 500 * 1024 * 1024
        if uid == "expired":
            # Deliberately an ISO *string*, not a datetime: records written
            # before the fix that stores a real date still live in Mongo that
            # way, and _is_expired() has to keep understanding both.
            meta["expires_at"] = "2000-01-01T00:00:00"
        if uid == "attached":
            # An explicitly attached subtitle (the /sub command) is stored as
            # a media.subtitles entry whose index is in the 200+ band and
            # whose src_uid points at the subtitle's own file record. The VTT
            # isn't on disk, so serving it must take the sidecar path — which
            # downloads via the (stubbed) client. With client=object() the
            # download fails, so the correct outcome is a clean 502, NOT a 500.
            # That distinction is the whole point: _serve_sidecar_subtitle
            # once crashed here with a NameError before it ever reached the
            # try/except, because file_uid wasn't threaded into its args.
            meta["media"] = {"subtitles": [
                {"index": 200, "label": "English", "language": "en",
                 "default": True, "src_uid": "subsrc"},
            ]}
        return meta

    async def get_recent_files(self, limit=5):
        # Fresh dicts each call: _decorate_card() writes its computed fields
        # onto the record it is given, which is a copy on both real backends.
        self.recent_calls += 1
        return [
            {"_id": "f1", "msg_id": 5, "file_size": 999,
             "file_name": "Clip One.mp4", "mime_type": "video/mp4",
             "type": "video", "saved_at": "2026-06-02T00:00:00"},
            {"_id": "expired", "msg_id": 6, "file_size": 1,
             "file_name": "Stale.mkv", "mime_type": "video/x-matroska",
             "type": "video", "saved_at": "2026-06-01T00:00:00",
             "expires_at": "2000-01-01T00:00:00"},
            {"file_name": "no uid.mkv", "type": "video"},
            # A locked upload must not appear on a public index at all — not
            # even as an anonymous placeholder, because "something was
            # uploaded on Tuesday" is itself a fact the owner hid.
            {"_id": "locked", "msg_id": 7, "file_size": 4242,
             "file_name": "Private LOCKED.mkv", "mime_type": "video/x-matroska",
             "type": "video", "saved_at": "2026-06-03T00:00:00",
             "lock": self.locks["locked"]},
        ][:limit]

    async def get_batch(self, bid):
        if bid == "bbig":
            # Same shape as bsub, but the sidecar is a 500 MB "subtitle".
            return {"status": "done", "files": ["bigvid", "bigsub"]}
        if bid == "bsub":
            # The sidecar fixture: a video plus two matching-stem subtitle
            # files, one of which is locked. Order matters — the public one
            # must land on index 100 so the URL the page emits is predictable.
            return {"status": "done", "files": ["movie", "suben", "subjp"]}
        # Four members: one expired (dropped from the grid entirely) and one
        # locked (kept, as an unnamed placeholder card — the batch link was
        # shared as a whole, so the file shouldn't vanish without trace).
        return {"status": "done", "files": ["f1", "f2", "expired", "locked"]}

    async def increment_stat(self, key):
        pass

    async def increment_file_stat(self, file_id, key, amount=1):
        pass

    async def set_file_media(self, file_id, manifest):
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

    db = FakeDB()
    app = webapp.create_app(client=object(), db=db, base_url="https://example.test")
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

        print("\nMedia tracks (subtitles / audio languages):")
        r = await client.get("/file/tracks")
        page = await r.text()
        check("GET /file/tracks -> 200", r.status == 200)
        check("Embedded subtitles rendered as <track> elements",
              'srclang="hi"' in page and 'src="/subs/tracks/1"' in page)
        check("Subtitle dropdown lists every track (not a hardcoded 'English')",
              '>Hindi</option>' in page and ">English</option>" in page)
        check("Audio dropdown offers Original + each extracted language",
              'value="-1" selected>Original' in page and ">Hindi</option>" in page)
        check("Player feeds AUDIO_SOURCES from the manifest",
              '/media/tracks/audio/2' in page)

        r = await client.get("/subs/tracks/99")
        check("GET /subs/<id>/99 (no such track) -> 404", r.status == 404)
        r = await client.get("/subs/tracks/1")
        check("GET /subs/<id>/1 (listed, not extracted) -> 404 not 500", r.status == 404)
        r = await client.get("/media/tracks/2")
        check("GET /media/<id>/audio/2 (no file on disk) -> 404", r.status == 404)
        r = await client.get("/subs/tracks/notanumber")
        check("GET /subs/<id>/<bad index> -> 404 not 500", r.status == 404)

        r = await client.post("/tracks/abc")
        body = await r.text()
        if webapp.media.available():
            check("POST /tracks/<id> -> 202 preparing",
                  r.status == 202 and "preparing" in body)
        else:
            check("POST /tracks/<id> -> honest 503 without FFmpeg",
                  r.status == 503 and "ffmpeg-unavailable" in body)

        print("\nRange / resume headers (fake file_size = 1234567):")
        # HEAD is answered from the headers alone, before the Telegram
        # pre-flight, so byte-range semantics are fully testable offline.
        SIZE = 1234567
        r = await client.head("/download/abc", headers={"Range": "bytes=0-99"})
        check("simple range -> 206", r.status == 206)
        check("simple range Content-Range",
              r.headers.get("Content-Range") == f"bytes 0-99/{SIZE}")
        check("simple range Content-Length", r.headers.get("Content-Length") == "100")

        r = await client.head("/download/abc", headers={"Range": "bytes=100-"})
        check("open-ended range runs to the end",
              r.headers.get("Content-Range") == f"bytes 100-{SIZE - 1}/{SIZE}")

        r = await client.head("/download/abc", headers={"Range": "bytes=-500"})
        # The last 500 bytes, NOT the first 500 — the old parser got this
        # backwards and handed clients the wrong half of the file.
        check("suffix range = last N bytes, not first N",
              r.headers.get("Content-Range") == f"bytes {SIZE - 500}-{SIZE - 1}/{SIZE}"
              and r.headers.get("Content-Length") == "500")

        r = await client.head("/download/abc", headers={"Range": f"bytes={SIZE + 5}-"})
        check("unsatisfiable range -> 416", r.status == 416)
        check("416 carries Content-Range: bytes */size",
              r.headers.get("Content-Range") == f"bytes */{SIZE}")

        etag = r.headers.get("ETag") or '"abc-1234567"'
        r = await client.head("/download/abc", headers={"If-None-Match": etag})
        check("If-None-Match -> 304", r.status == 304)

        r = await client.head("/download/abc", headers={
            "Range": "bytes=0-99", "If-Range": '"a-different-etag-1234567"'})
        check("Range ignored when If-Range doesn't match -> full 200",
              r.status == 200 and "Content-Range" not in r.headers)

        # Content-Range is only legal on 206/416; a full-file 200 must not
        # send it.
        r = await client.head("/download/abc")
        check("plain full-file 200 does NOT carry Content-Range",
              r.status == 200 and "Content-Range" not in r.headers
              and r.headers.get("Content-Length") == str(SIZE))
        check("Accept-Ranges advertised on every response",
              r.headers.get("Accept-Ranges") == "bytes")

        print("\nSRT -> WebVTT converter (pure Python, sidecar subtitles):")
        # These fixtures are the real-world SRT dialect variations that a
        # naive converter gets wrong. Each one used to silently lose cues:
        # a timing line the browser can't parse drops the ENTIRE cue, not
        # just its positioning, so the subtitle simply never appears.
        _srt = webapp.media.srt_to_vtt
        _cases = [
            ("plain SRT",
             "1\n00:00:01,000 --> 00:00:04,500\nHello world\n", "00:00:01.000 --> 00:00:04.500"),
            ("short millisecond field is ms, not a fraction",
             "1\n00:00:01,50 --> 00:00:04,5\nx\n", "00:00:01.050 --> 00:00:04.005"),
            ("positioning BEFORE the arrow (Aegisub/Karafel style)",
             "1\n00:00:01,000 X1:16 X2:60 Y1:10 Y2:35 --> 00:00:02,000\nx\n",
             "00:00:01.000 --> 00:00:02.000"),
            ("legal VTT settings AFTER the arrow are kept",
             "1\n00:00:01,000 --> 00:00:02,000 line:16% align:start\nx\n",
             "00:00:01.000 --> 00:00:02.000 line:16% align:start"),
            ("SRT-only tokens stripped, VTT ones kept",
             "1\n00:00:01,000 X1:16 --> 00:00:02,000 line:16% Y1:9\nx\n",
             "00:00:01.000 --> 00:00:02.000 line:16%"),
            ("ASS override block stripped from cue text",
             "1\n00:00:01,000 --> 00:00:02,000\n{\\an8}Top aligned\n", "Top aligned"),
            ("CRLF line endings",
             "1\r\n00:00:01,000 --> 00:00:02,000\r\ntext\r\n", "00:00:01.000 --> 00:00:02.000"),
            ("UTF-8 BOM",
             "﻿1\n00:00:01,000 --> 00:00:02,000\ntext\n", "00:00:01.000 --> 00:00:02.000"),
            ("already-dot timestamps",
             "1\n00:00:01.000 --> 00:00:02.000\ntext\n", "00:00:01.000 --> 00:00:02.000"),
            ("single-digit hour padded",
             "1\n1:00:01,000 --> 1:00:02,000\ntext\n", "01:00:01.000 --> 01:00:02.000"),
        ]
        for label, src, want in _cases:
            out = _srt(src)
            # Both halves matter: the timing line must convert, AND the cue's
            # text must still be there (a mis-parsed timing line silently
            # drops the whole cue, text included).
            check(f"{label}", want in out and len(out.strip().splitlines()) > 2)

        # Every emitted timing line must be something a browser parses.
        import re as _re
        _ts_re = _re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}( [a-zA-Z]+:\S+)*$")
        _all_ok = all(
            _ts_re.match(line)
            for _label, src, _want in _cases
            for line in _srt(src).splitlines() if "-->" in line
        )
        check("every timing line is well-formed WebVTT", _all_ok)
        check("output always carries the WEBVTT header",
              all(_srt(src).startswith("WEBVTT") for _l, src, _w in _cases))
        for _bad in ["", "\n", "not subtitles at all"]:
            check("degenerate input still yields a valid header",
                  _srt(_bad).startswith("WEBVTT"))

        print("\nASS/SSA → WebVTT converter:")
        _ass = webapp.media.ass_to_vtt
        _vtt = _ass(
            "[Script Info]\nTitle: Demo\n\n"
            "[V4+ Styles]\nFormat: Name\nStyle: Default\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.50,0:00:04.00,Default,,0,0,0,,Hello world\n"
            r"Dialogue: 0,0:00:05.10,0:00:07.99,Default,,0,0,0,,{\an8}Top line{\pos(10,10)}" "\n"
            r"Dialogue: 0,0:00:10.00,0:00:12.00,Default,,0,0,0,,{\p1}m 0 0 l 10 10{\p0}" "\n"
            "Dialogue: 0,0:00:13.00,0:00:15.00,Default,,0,0,0,,Comma, inside text\n"
            "Comment: 0,0:00:16.00,0:00:17.00,Default,,0,0,0,,should be skipped\n"
        )
        check("ASS header/Styles sections produce no cues",
              _vtt.count("-->") == 3)
        check("centiseconds become milliseconds, not a decimal",
              "00:00:01.500 --> 00:00:04.000" in _vtt)
        check("\\an8 becomes a top-aligned cue",
              "line:10% align:middle" in _vtt)
        check("override blocks are stripped from cue text",
              r"{\p1}" not in _vtt and "m 0 0 l 10 10" not in _vtt)
        check("a comma inside the text field doesn't shift the parse",
              "Comma, inside text" in _vtt)
        check("Comment: lines are not turned into cues",
              "should be skipped" not in _vtt)
        check("timing lines are well-formed WebVTT",
              all(_ts_re.match(ln) for ln in _vtt.splitlines() if "-->" in ln))
        _legacy = _ass(
            "[Events]\n"
            "Format: Marked, Start, End, Style, Name, Text\n"
            "Dialogue: Marked=0,0:00:02.00,0:00:03.00,Default,,Legacy SSA text\n"
        )
        check("the file's own Format: line drives the field order",
              "Legacy SSA text" in _legacy and "00:00:02.000" in _legacy)
        check("a non-ASS body produces a header with no cues",
              _ass("not an ass file at all").strip() == "WEBVTT")
        # .ass used to go through the SRT parser, which found no timestamp
        # blocks in a sectioned document and returned a valid-but-EMPTY track.
        _body = "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hi\n"
        check("srt_to_vtt on ASS really is empty — the reason dispatch exists",
               _srt(_body).count("-->") == 0)
        check("sidecar_to_vtt routes .ass to the ASS parser",
               webapp.media.sidecar_to_vtt(_body, "ass").count("-->") == 1)
        check("sidecar_to_vtt passes an existing .vtt through untouched",
               webapp.media.sidecar_to_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n", "vtt")
               .startswith("WEBVTT\n\n00:00:01.000"))

        print("\nSubtitle language hints from filenames (/sub):")
        _lang = webapp.media.language_from_name
        check("a 3-letter token is read", _lang("Movie.2024.eng.subs.srt") == "eng")
        check("a bracketed 2-letter code is read", _lang("Movie.[hi].ass") == "hi")
        check("a bare caption file yields no language", _lang("captions.srt") == "")
        check("a short token that isn't a known code isn't guessed",
              _lang("Movie.ts.srt") == "")
        check("codes map to the same display names FFmpeg tracks use",
              webapp.media.language_name("hin") == "Hindi"
              and webapp.media.language_name("") == "")
        check("only subtitle extensions are recognised as sidecars",
              webapp.media.has_sidecar_subtitle("a.ass") == "ass"
              and webapp.media.has_sidecar_subtitle("a.mkv") is None)

        print("\nBatch page & link expiry:")
        r = await client.get("/batch/B1")
        batch_html = await r.text()
        check("GET /batch/<id> -> 200", r.status == 200)
        # The fixture batch has four members: one expired (not counted), one
        # locked (counted, as a placeholder).
        check("Expired member is NOT counted in the batch total",
              'data-count="3"' in batch_html)
        check("Expired member's link is not offered on the batch page",
              "/file/expired" not in batch_html)
        check("Live members still are", "/file/f1" in batch_html)
        check("A locked member keeps its slot so the batch count stays true",
              "/file/locked" in batch_html and "Password protected" in batch_html)
        check("…but its card names nothing: no filename, no size, no type",
              "Private LOCKED" not in batch_html and "4.1 KB" not in batch_html)
        check("…and offers no download or stream link, only the password page",
              "/download/locked" not in batch_html and "/stream/locked" not in batch_html
              and "Enter password" in batch_html)
        check("Bulk download excludes locked members",
              "Download all (2)" in batch_html)

        r = await client.get("/file/expired")
        check("GET /file/expired -> 410 Gone", r.status == 410)
        r = await client.get("/info/expired")
        body = await r.text()
        check("/info reports expired=True for a string-typed expires_at",
              '"expired": true' in body or '"expired":true' in body)

        print("\nAttached subtitle track (/sub):")
        # idx 200 lives in the manifest with a src_uid, so the request gets
        # past the track lookup and has to download the subtitle file itself.
        # The stubbed client can't do that, so the honest answer is 502 —
        # a 500 here means the handler blew up before reaching its own
        # try/except, which is exactly the NameError it used to have.
        r = await client.get("/subs/attached/200")
        check("GET /subs/<id>/200 reaches the sidecar path", r.status != 404)
        check("Failed subtitle fetch degrades to 502, not a 500",
              r.status == 502)
        r = await client.get("/subs/attached/201")
        check("An unassigned 200-band index is a clean 404", r.status == 404)

        print("\nPassword-protected links (/lock):")
        # Must also stay ahead of the rate limiter: these hit /file/, which
        # shares that one per-IP budget.
        utils = sys.modules["utils"]
        r = await client.get("/file/locked")
        prompt = await r.text()
        check("GET /file/<locked> -> 401 with a password prompt",
              r.status == 401 and "Enter the password" in prompt)
        check("the prompt leaks nothing about the file",
              "Private LOCKED" not in prompt and "1.2 MB" not in prompt)
        check("the prompt is noindex and has no canonical",
              'content="noindex, nofollow"' in prompt and 'rel="canonical"' not in prompt)
        check("its form posts back to the link itself, as a password field",
              'action="/file/locked"' in prompt and 'name="password"' in prompt)

        for path in ("/stream/locked", "/download/locked", "/info/locked",
                     "/thumbnail/locked", "/subs/locked/0", "/media/locked/audio/0"):
            r = await client.get(path)
            check(f"GET {path} -> 401 instead of the file", r.status == 401)
        r = await client.head("/download/locked")
        check("HEAD /download/locked -> 401 (so batch 'download all' can't peek)",
              r.status == 401)
        r = await client.post("/tracks/locked")
        check("POST /tracks/locked -> 401 (no extraction on a locked file)", r.status == 401)

        r = await client.post("/file/locked", data={"password": "guess"},
                              allow_redirects=False)
        wrong = await r.text()
        check("a wrong password is refused", r.status == 401 and "Wrong password" in wrong)
        check("…naming nothing, and counting the tries left",
              "Private LOCKED" not in wrong and "attempt" in wrong)

        r = await client.post("/file/locked", data={"password": "hunter2"},
                              allow_redirects=False)
        set_cookie = r.headers.get("Set-Cookie", "")
        check("the right password redirects to the page", r.status == 303)
        check("…setting an HttpOnly, SameSite=Lax cookie",
              "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie)
        check("…scoped to this one link, and for a week",
              "flk_locked=" in set_cookie and "Max-Age=604800" in set_cookie)
        # "flk_locked=<token>; Max-Age=…; HttpOnly" → just the pair we need.
        name, _, rest = set_cookie.partition("=")
        cookie = {name: rest.partition(";")[0]}

        r = await client.get("/file/locked", cookies=cookie)
        page = await r.text()
        check("the cookie opens the page and shows the file",
              r.status == 200 and "Private LOCKED.mkv" in page)
        r = await client.get("/download/locked", cookies=cookie,
                             headers={"Range": "bytes=0-99"})
        # 404, not 206: the stubbed Telegram client can't resolve the stored
        # message. What's being asserted is that the lock no longer stops the
        # request — it got all the way to the (fake) source lookup.
        check(f"…and the byte routes with it (got {r.status}, must not be 401)",
              r.status != 401)
        r = await client.get("/info/locked", cookies=cookie)
        body = await r.text()
        check("…plus /info, which must still withhold the hash record",
              r.status == 200 and '"lock"' not in body and "pbkdf2" not in body)
        r = await client.get("/file/locked2", cookies=cookie)
        check("a cookie for one link doesn't open another", r.status == 401)

        raw = list(cookie.values())[0]
        tampered = {list(cookie)[0]: raw[:-1] + ("0" if raw[-1] != "0" else "1")}
        r = await client.get("/file/locked", cookies=tampered)
        check("flipping a byte of the cookie breaks the signature", r.status == 401)

        db.locks["locked"] = utils.hash_password("hunter2")  # same pw, new salt
        r = await client.get("/file/locked", cookies=cookie)
        check("re-locking invalidates every cookie issued before it", r.status == 401)
        r = await client.post("/file/locked", data={"password": "hunter2"},
                              allow_redirects=False)
        check("…and the same password mints a working one", r.status == 303)
        name, _, rest = r.headers.get("Set-Cookie", "").partition("=")
        cookie = {name: rest.partition(";")[0]}

        statuses = []
        for _ in range(9):
            rr = await client.post("/file/locked", data={"password": "guess"},
                                   allow_redirects=False)
            statuses.append(rr.status)
        check("guessing is capped at 8, not an unlimited KDF burn",
              statuses.count(401) == 8 and statuses.count(429) == 1)
        check("…with a Retry-After so the form can count down",
              rr.headers.get("Retry-After", "").isdigit())
        r = await client.post("/file/locked2", data={"password": "guess"},
                              allow_redirects=False)
        check("the lockout is per link: a different file still answers",
              r.status == 401)
        r = await client.post("/file/locked2", data={"password": "   "},
                              allow_redirects=False)
        check("a blank password is a 400, and doesn't cost an attempt",
              r.status == 400 and "Enter the password first" in await r.text())
        r = await client.post("/file/locked2", data=b"x" * 20000,
                              allow_redirects=False)
        check("a huge form body is refused rather than buffered", r.status == 400)

        r = await client.post("/file/abc", data={"password": "anything"},
                              allow_redirects=False)
        check("POST to an unprotected link just goes to the page", r.status == 303)
        r = await client.post("/file/missing", data={"password": "x"},
                              allow_redirects=False)
        check("POST /file/missing -> 404", r.status == 404)
        r = await client.post("/file/expired", data={"password": "x"},
                              allow_redirects=False)
        check("POST /file/expired -> 410", r.status == 410)

        r = await client.get("/subs/subparent/200")
        check("a sidecar with its own password is not served via its parent",
              r.status == 404)

        print("\nSidecar subtitles discovered from a batch (uploaded .srt files):")
        r = await client.get("/file/movie")
        page = await r.text()
        check("GET /file/movie -> 200", r.status == 200)
        check("an uploaded .srt sibling becomes exactly one <track>",
              page.count('src="/subs/movie/') == 1)
        check("…pointing at the /subs converter, not the raw .srt",
              '/subs/movie/100' in page)
        check("…and the template gets a real src, not the empty string it "
              "used to render", 'src=""' not in page)
        check("a locked sibling is not named on the public page",
              "JP" not in page and "/subs/movie/101" not in page)
        r = await client.get("/subs/movie/100")
        check("the sidecar index the page emits actually resolves",
              r.status != 404)
        r = await client.get("/file/bigvid")
        big_page = await r.text()
        check("an over-limit batch sidecar is not offered as a track",
              "/subs/bigvid/100" not in big_page)
        check("…while the manifest's own 200-band track still reaches the page",
              "/subs/bigvid/200" in big_page)
        r = await client.get("/subs/bigvid/200")
        check("…and requesting that one directly is a 413, not a 500 MB download",
              r.status == 413)

        print("\nSpoofable proxy headers (web/security._client_ip):")
        # The header is the key both the guess cap and the rate limiter bucket
        # on, so if a client can choose it, neither limit means anything —
        # 9 guesses would be 9 separate budgets and an unbounded KDF burn.
        sec = sys.modules["web.security"]
        lockmod = sys.modules["web.locks"]
        lockmod._ATTEMPTS.clear()
        os.environ["TRUSTED_PROXY_CIDRS"] = "203.0.113.0/24"
        codes = []
        for i in range(9):
            rr = await client.post("/file/locked3", data={"password": "guess"},
                                   headers={"X-Forwarded-For": f"10.{i}.0.1"},
                                   allow_redirects=False)
            codes.append(rr.status)
        del os.environ["TRUSTED_PROXY_CIDRS"]
        check("a spoofed X-Forwarded-For can't buy a fresh guess budget",
              codes.count(401) == 8 and codes.count(429) == 1)
        lockmod._ATTEMPTS.clear()
        r = await client.post("/file/locked4", data={"password": "guess"},
                              headers={"X-Forwarded-For": "8.8.8.8, 9.9.9.9"},
                              allow_redirects=False)
        keys = [k for k in lockmod._ATTEMPTS if k.endswith(":locked4")]
        check("…but behind a proxy the rightmost hop is still the client, so "
              "real users don't share one bucket",
              r.status == 401 and bool(keys) and keys[0].startswith("9.9.9.9:"))
        lockmod._ATTEMPTS.clear()

        print("\nPublic gallery (/latest):")
        # Must stay BEFORE the rate-limiter block: /info shares the one
        # per-IP 120/min budget, and that block burns it to a 429.
        r = await client.get("/latest")
        check("GET /latest -> 404 while public_gallery is off", r.status == 404)
        r = await client.get("/")
        home_off = await r.text()
        # Matched on the strip's own markup, not the words "Recently shared" —
        # those also appear in the page's <style> block, which is shipped
        # whether or not the section renders.
        check("landing page renders no strip while off", 'href="/latest"' not in home_off)
        check("and issues no database query at all while off", db.recent_calls == 0)

        sys.modules["info"]._gallery["on"] = True
        r = await client.get("/latest")
        gal = await r.text()
        check("GET /latest -> 200 once enabled", r.status == 200)
        check("live upload is listed", "/file/f1" in gal)
        check("expired upload is not listed", "/file/expired" not in gal)
        check("a password-protected upload isn't listed at all, not even as "
              "an anonymous placeholder", "/file/locked" not in gal)
        check("a record with no uid is skipped, not rendered as /file/None",
              "/file/None" not in gal and "no uid.mkv" not in gal)
        check("cards carry download and stream links",
              "/download/f1" in gal and "/stream/f1" in gal)
        check("gallery page is noindex", 'content="noindex, follow"' in gal)
        check("no canonical on a rotating list", 'rel="canonical"' not in gal)

        before = db.recent_calls
        await client.get("/latest")
        await client.get("/")
        check("the 30s cache means repeat hits cost no extra query",
              db.recent_calls == before)

        r = await client.get("/")
        home_on = await r.text()
        check("landing page gains the recently-shared strip", 'class="recent-rail"' in home_on)
        check("strip links to the full page", 'href="/latest"' in home_on)

        webapp._latest_cache = (0.0, [])
        sys.modules["info"]._gallery["on"] = False
        r = await client.get("/latest")
        check("turning it back off 404s without a restart", r.status == 404)
        r = await client.get("/")
        check("and the strip is gone again", 'class="recent-rail"' not in await r.text())

        r = await client.get("/robots.txt")
        check("robots.txt disallows /latest", "Disallow: /latest" in await r.text())
        r = await client.get("/sitemap.xml")
        check("sitemap never advertises /latest", "/latest" not in await r.text())

        print("\nRate limiter (/info, 120/min):")
        # One shared per-IP budget covers /file, /batch, /info and /tracks,
        # and the lock tests above legitimately spent part of it on /file/.
        # Start this block from an empty bucket so it measures the limit
        # itself instead of the suite's cumulative traffic.
        sys.modules["web.security"]._RL_BUCKETS.clear()
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
