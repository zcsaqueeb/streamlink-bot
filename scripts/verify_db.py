#!/usr/bin/env python3
"""
verify_db.py — offline smoke test for the database layer and shared file helpers.

verify_web.py can't reach any of this: it replaces the whole DB with a stub, so
the attach/detach index bands, the rename/uploader queries, the expiry type
handling, the password hashing /lock stores and the delete-cleanup helper have
never had a single check run against them — every one was written and then only
re-read by eye. This exercises the real Database and the real utils with only
what surrounds them replaced (pyrogram, the Telegram config, and web.media's
disk cleanup).

Usage (from the repo root):
    python scripts/verify_db.py

Exit code 0 = all checks passed, 1 = a check failed.
"""
import asyncio
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# LOCAL_DB_PATH is read at import time, so this has to be set first — every
# write below goes to a throwaway temp file, never to a real local_db.json.
SNAP = os.path.join(tempfile.mkdtemp(prefix="verifydb-"), "local_db.json")
os.environ["LOCAL_DB_PATH"] = SNAP
os.environ.setdefault("BOT_TOKEN", "123456:AAstubtokenforimportonly")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "stubhash")

# Records every web.media.cleanup_dir() call so the delete tests can assert
# which directories were reclaimed, in order.
CLEANED: list = []

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


def _install_stubs():
    sys.path.insert(0, HERE)

    # Pyrogram cannot even be imported on this Python (its sync wrapper calls
    # asyncio.get_event_loop() at import time, removed in 3.12+), and nothing
    # under test touches it — utils needs these three names to exist at import.
    pg = types.ModuleType("pyrogram")
    pg.Client = type("Client", (), {})
    pg_types = types.ModuleType("pyrogram.types")
    pg_types.Message = type("Message", (), {})
    pg_enums = types.ModuleType("pyrogram.enums")

    class ChatMemberStatus:
        MEMBER = "member"
        ADMINISTRATOR = "administrator"
        OWNER = "owner"

    pg_enums.ChatMemberStatus = ChatMemberStatus
    pg.types, pg.enums = pg_types, pg_enums
    sys.modules.update({"pyrogram": pg, "pyrogram.types": pg_types,
                        "pyrogram.enums": pg_enums})

    # utils.delete_file_completely() imports web.media lazily — the real one
    # would build the whole aiohttp app. cleanup_dir() is all it uses.
    web = types.ModuleType("web")
    media = types.ModuleType("web.media")

    def cleanup_dir(file_uid):
        CLEANED.append(file_uid)

    media.cleanup_dir = cleanup_dir
    media.available = lambda: False
    web.media = media
    sys.modules.update({"web": web, "web.media": media})

    import database.database as dbmod
    import utils
    return dbmod, utils


def manifest(subs, **extra):
    return {"subtitles": subs, **extra}


async def run():
    dbmod, utils = _install_stubs()
    Database = dbmod.Database
    BASE = Database._ATTACHED_SUB_BASE
    base = datetime(2026, 6, 1, 12, 0, 0)

    # ── attach_subtitle: the 200-255 band ────────────────────────────────────
    print("\nattach_subtitle index band:")
    db = Database()
    await db.save_file("vid", {
        "msg_id": 7, "file_name": "Movie.mkv", "type": "video",
        "media": manifest(
            [{"index": 0, "label": "English", "src_uid": "ff0"},
             {"index": 100, "label": "Sidecar", "src_uid": "sc"}],
            audio=[{"index": 2, "label": "Hindi"}], duration=1200.0,
        ),
    })
    check("target that doesn't exist -> None",
          (await db.attach_subtitle("nope", "s1")) is None)
    i0 = await db.attach_subtitle("vid", "s1", "English", "en")
    check(f"first /sub track lands on {BASE}, not on a free low index", i0 == BASE)
    i1 = await db.attach_subtitle("vid", "s2", "Hindi", "hi")
    check("second takes the next free index in the band", i1 == BASE + 1)
    m = await db.get_file("vid")
    subs = m["media"]["subtitles"]
    check("pre-existing FFmpeg + sidecar entries are kept",
          [s["index"] for s in subs] == [0, 100, BASE, BASE + 1])
    check("unrelated manifest keys (audio, duration) survive",
          m["media"].get("audio") == [{"index": 2, "label": "Hindi"}]
          and m["media"].get("duration") == 1200.0)
    check("label/language/default stored on the new entry",
          subs[2] == {"index": BASE, "label": "English", "language": "en",
                      "default": False, "src_uid": "s1"})
    i2 = await db.attach_subtitle("vid", "s3")
    check("empty label falls back to 'Subtitles'",
          (await db.get_file("vid"))["media"]["subtitles"][4]["label"] == "Subtitles")
    check("first attached track can be flagged default",
          (await db.attach_subtitle("vid", "s4", "Español", "es", default=True))
          is not None)
    check("default flag round-trips",
          (await db.get_file("vid"))["media"]["subtitles"][5]["default"] is True)
    check("i2 used the next slot", i2 == BASE + 2)

    # Fill the band completely, then ask for one more.
    packed = [{"index": BASE + n, "label": f"L{n}", "src_uid": f"p{n}"}
              for n in range(56)]
    await db.set_file_media("vid", manifest(packed))
    check("band full -> None instead of a colliding index",
          (await db.attach_subtitle("vid", "overflow")) is None)
    check("a refused attach leaves the manifest untouched",
          len((await db.get_file("vid"))["media"]["subtitles"]) == 56)

    # ── attached_subtitles: which entries count as "/sub made this" ──────────
    print("\nattached_subtitles filtering:")
    meta = await db.get_file("vid")
    await db.set_file_media("vid", manifest(
        [{"index": 0, "src_uid": "ff0"}, {"index": 100, "src_uid": "sc"},
         {"index": BASE, "src_uid": "s1"}, {"index": BASE + 1, "src_uid": "s2"},
         {"index": BASE + 2}]))
    meta = await db.get_file("vid")
    check("only the /sub band is returned (FFmpeg + sidecar excluded)",
          db.attached_subtitles(meta) == ["s1", "s2"])
    check("band entry with no src_uid is skipped", "s3" not in db.attached_subtitles(meta))
    check("no manifest -> []", db.attached_subtitles({"msg_id": 1}) == [])
    check("empty meta -> []", db.attached_subtitles({}) == [])
    check("None meta -> []", db.attached_subtitles(None) == [])
    try:
        weird = db.attached_subtitles({"media": None})
        ok = weird == []
        boom = ""
    except Exception as e:
        ok, boom = False, f"{type(e).__name__}: {e}"
    check(f"a stored media:null is tolerated{'' if ok else ' — ' + boom}", ok)

    # ── detach_subtitle ──────────────────────────────────────────────────────
    print("\ndetach_subtitle refuses outside its band:")
    before = [s["index"] for s in (await db.get_file("vid"))["media"]["subtitles"]]
    check("missing file -> (False, None)",
          await db.detach_subtitle("nope", BASE) == (False, None))
    check("FFmpeg track (index 0) refused", await db.detach_subtitle("vid", 0) == (False, None))
    check("batch sidecar (index 100) refused", await db.detach_subtitle("vid", 100) == (False, None))
    check("index that isn't a subtitle refused", await db.detach_subtitle("vid", 999) == (False, None))
    after = [s["index"] for s in (await db.get_file("vid"))["media"]["subtitles"]]
    check("none of the refusals changed the manifest", before == after)
    check("real detach returns (True, src_uid)",
          await db.detach_subtitle("vid", BASE) == (True, "s1"))
    check("only that one entry is gone, order preserved",
          [s["index"] for s in (await db.get_file("vid"))["media"]["subtitles"]]
          == [0, 100, BASE + 1, BASE + 2])
    check("detaching the same index twice is refused",
          await db.detach_subtitle("vid", BASE) == (False, None))
    # Separate record: the band-fill above went through set_file_media(), which
    # replaces the whole manifest by design, so audio/duration are no longer
    # present here to assert on.
    await db.save_file("vid2", {"file_name": "Two.mkv", "type": "video",
                                "media": manifest([{"index": BASE, "src_uid": "q1"}],
                                                  audio=[{"index": 2}], duration=60.0)})
    check("detach leaves the rest of the manifest (audio/duration) alone",
          await db.detach_subtitle("vid2", BASE) == (True, "q1")
          and (await db.get_file("vid2"))["media"] == {"subtitles": [],
                                                       "audio": [{"index": 2}],
                                                       "duration": 60.0})

    # ── rename_file ──────────────────────────────────────────────────────────
    print("\nrename_file:")
    db2 = Database()
    await db2.save_file("vid", {"msg_id": 7, "file_name": "Old Name.mkv",
                                "type": "video", "file_size": 42,
                                "media": manifest([{"index": BASE, "src_uid": "s1"}])})
    check("missing file -> False", not await db2.rename_file("nope", "X"))
    check("rename -> True", await db2.rename_file("vid", "Correct Name.mkv"))
    f = await db2.get_file("vid")
    check("name changed", f["file_name"] == "Correct Name.mkv")
    check("everything else untouched (media, msg_id, size)",
          f["media"]["subtitles"] == [{"index": BASE, "src_uid": "s1"}]
          and f["msg_id"] == 7 and f["file_size"] == 42)
    reloaded = Database()
    reloaded._load_local()
    check("rename reaches the disk snapshot",
          (reloaded._files.get("vid") or {}).get("file_name") == "Correct Name.mkv")

    # ── get_files_by_uploader ────────────────────────────────────────────────
    print("\nget_files_by_uploader:")
    db3 = Database()
    # Seeded directly because save_file() always stamps saved_at=now, which
    # would make "newest first" untestable — and deliberately inserted oldest
    # first so a plain insertion-order return would fail the sort check.
    db3._files = {
        "old": {"uploader_id": 1, "file_name": "old.mkv", "saved_at": base},
        "new": {"uploader_id": 1, "file_name": "new.mkv", "saved_at": base + timedelta(days=2)},
        "nosave": {"uploader_id": 1, "file_name": "no saved_at.mkv"},
        "theirs": {"uploader_id": 2, "file_name": "other.mkv", "saved_at": base + timedelta(days=9)},
        "anon": {"file_name": "no uploader.mkv", "saved_at": base},
    }
    mine = await db3.get_files_by_uploader(1)
    check("newest first", [x["file_name"] for x in mine]
          == ["new.mkv", "old.mkv", "no saved_at.mkv"])
    check("_id exposed for link building", [x["_id"] for x in mine] == ["new", "old", "nosave"])
    check("other uploaders excluded", all(x["uploader_id"] == 1 for x in mine))
    check("limit honoured", len(await db3.get_files_by_uploader(1, limit=1)) == 1)
    check("a record with no saved_at doesn't raise", len(mine) == 3)
    check("user with nothing -> []", await db3.get_files_by_uploader(99) == [])
    check("limit larger than the result set", len(await db3.get_files_by_uploader(1, limit=50)) == 3)

    # ── expiry helpers (utils) ───────────────────────────────────────────────
    print("\nexpiry_datetime / is_expired type handling:")
    ed, ie = utils.expiry_datetime, utils.is_expired
    naive_past = datetime.utcnow() - timedelta(days=1)
    naive_future = datetime.utcnow() + timedelta(days=1)
    aware_past = datetime.now(timezone.utc) - timedelta(days=1)
    aware_offset = datetime(2026, 1, 1, 0, 0, 0,
                            tzinfo=timezone(timedelta(hours=5, minutes=30)))
    check("None meta -> None", ed(None) is None)
    check("no expires_at -> None", ed({}) is None)
    check("empty string -> None", ed({"expires_at": ""}) is None)
    check("unparsable string -> None", ed({"expires_at": "tomorrow-ish"}) is None)
    check("wrong type (int) -> None", ed({"expires_at": 1748736000}) is None)
    check("naive datetime passed through unchanged",
          ed({"expires_at": naive_past}) == naive_past)
    check("aware datetime normalized to naive UTC",
          ed({"expires_at": aware_past}) == aware_past.astimezone(timezone.utc)
          .replace(tzinfo=None) and ed({"expires_at": aware_past}).tzinfo is None)
    check("ISO string with an offset parsed and normalized",
          ed({"expires_at": "2026-01-01T00:00:00+05:30"})
          == datetime(2025, 12, 31, 18, 30, 0))
    check("aware datetime -> ISO string round trip stable",
          ed({"expires_at": aware_offset.isoformat()})
          == datetime(2025, 12, 31, 18, 30, 0))
    check("past -> expired", ie({"expires_at": naive_past}) is True)
    check("future -> not expired", ie({"expires_at": naive_future}) is False)
    check("no expiry -> never expired", ie({"expires_at": None}) is False)
    try:
        r = ie({"expires_at": aware_past})
        ok, boom = r is True, ""
    except TypeError as e:
        ok, boom = False, f"raised TypeError ({e})"
    check(f"aware value compares without raising{boom and ' — ' + boom}", ok)
    check("garbage value -> not expired (fail open, not 500)",
          ie({"expires_at": "not-a-date"}) is False)

    # ── snapshot round trip + the expiry sweep ───────────────────────────────
    print("\nJSON snapshot round trip and expiry sweep:")
    now_iso = datetime.utcnow().isoformat()
    import json
    with open(SNAP, "w", encoding="utf-8") as fh:
        json.dump({
            "files": {
                "aware": {"file_name": "aware.mkv",
                          "expires_at": (datetime.now(timezone.utc) - timedelta(days=3))
                          .astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()},
                "naive": {"file_name": "naive.mkv",
                          "expires_at": (datetime.utcnow() - timedelta(days=3)).isoformat()},
                "future": {"file_name": "future.mkv",
                           "expires_at": (datetime.utcnow() + timedelta(days=30)).isoformat()},
                "plain": {"file_name": "plain.mkv"},
            },
            "batches": {"b1": {"creator_id": 1, "files": ["plain"],
                               "status": "collecting", "created_at": now_iso}},
            "users": {"42": {"name": "Ada", "joined": now_iso, "banned": False,
                            "last_seen": now_iso, "warnings": 0, "temp_ban_until": None}},
            "stats": {"links_generated": 7, "files_uploaded": 3,
                      "streams_served": 11, "downloads_served": 5},
            "sessions": {"42": now_iso},
        }, fh)
    db4 = Database()
    db4._load_local()
    check("offset-bearing expires_at loaded as naive datetime",
          isinstance(db4._files["aware"]["expires_at"], datetime)
          and db4._files["aware"]["expires_at"].tzinfo is None)
    try:
        removed = await db4.delete_expired_files()
        boom = ""
        ok = removed == 2
    except TypeError as e:
        # This is the bug the normalization in _revive() exists to prevent:
        # comparing utcnow() against a tz-aware value aborts the whole sweep,
        # so every expired link stays reachable.
        removed, boom, ok = None, f"raised TypeError ({e})", False
    check(f"expiry sweep removes exactly the two stale rows{boom and ' — ' + boom}", ok)
    check("unexpired and never-expiring rows survive",
          set(db4._files) == {"future", "plain"})
    check("batches/users/stats/sessions all restored from the snapshot",
          "b1" in db4._batches and 42 in db4._users
          and (await db4.get_stats())["links_generated"] == 7)
    check("active-session map survives a restart",
          await db4.active_users_count(minutes=60) == 1)
    check("user ids come back as ints, not strings",
          list(db4._users) == [42])
    await db4.save_file("fresh", {"file_name": "fresh.mkv", "type": "video"})
    db5 = Database()
    db5._load_local()
    check("save_file after a reload keeps the surviving rows and adds the new one",
          set(db5._files) == {"future", "plain", "fresh"})
    check("saved_at round-trips as a real datetime",
          isinstance(db5._files["fresh"]["saved_at"], datetime))
    check("non-datetime fields are not mangled",
          db5._files["fresh"]["file_name"] == "fresh.mkv")

    # ── delete_file_completely ───────────────────────────────────────────────
    print("\ndelete_file_completely (shared by /delete and /unlink):")
    db6 = Database()
    for uid, meta in [
        ("vid", {"file_name": "Movie.mkv", "type": "video",
                 "media": manifest([
                     {"index": 0, "label": "English"},
                     {"index": 100, "label": "Sidecar", "src_uid": "sc"},
                     {"index": BASE, "label": "Sub A", "src_uid": "s1"},
                     {"index": BASE + 1, "label": "Sub B", "src_uid": "s2"}])}),
        ("s1", {"file_name": "a.srt", "type": "document"}),
        ("s2", {"file_name": "b.srt", "type": "document"}),
        ("sc", {"file_name": "Movie.eng.srt", "type": "document"}),
    ]:
        await db6.save_file(uid, meta)

    CLEANED.clear()
    res = await utils.delete_file_completely(db6, "missing")
    check("missing file -> (False, 0)", res == (False, 0))
    check("nothing on disk is touched for a file that wasn't deleted", CLEANED == [])
    res = await utils.delete_file_completely(db6, "vid")
    check("video -> (True, 2 attached subtitles)", res == (True, 2))
    check("video and both /sub records are gone",
          not await db6.get_file("vid") and not await db6.get_file("s1")
          and not await db6.get_file("s2"))
    check("the batch sidecar sibling survives (it isn't ours to delete)",
          await db6.get_file("sc") is not None)
    check("track dirs cleaned for the video then each subtitle, in order",
          CLEANED == ["vid", "s1", "s2"])
    check("deleting twice doesn't double-clean or crash",
          await utils.delete_file_completely(db6, "vid") == (False, 0) and CLEANED == ["vid", "s1", "s2"])
    CLEANED.clear()
    await utils.delete_file_completely(db6, "sc")
    check("a plain file with no attached tracks still gets its dir reclaimed",
          CLEANED == ["sc"])

    print("\npassword hashes (utils — what /lock stores):")
    import base64
    import hashlib as _hashlib
    rec = utils.hash_password("hunter2")
    check("the record holds the kdf, salt and digest — never the password",
          set(rec) == {"kdf", "iterations", "salt", "hash"}
          and "hunter2" not in repr(rec))
    check("the iteration count comes from the module constant",
          rec["iterations"] == utils.PBKDF2_ITERATIONS)
    check("salt is 16 bytes and digest 32, both base64",
          len(base64.b64decode(rec["salt"])) == 16
          and len(base64.b64decode(rec["hash"])) == 32)
    check("the same password twice gives a different record (salted, not a bare digest)",
          utils.hash_password("hunter2")["salt"] != rec["salt"])

    # A hand-built record at 1,000 iterations: verify_password() honours the
    # count stored *in* the record, so the behaviour under test is identical
    # and the suite doesn't spend ten seconds inside hashlib.
    def cheap(pw, salt=b"0123456789abcdef", iters=1000):
        return {"kdf": "pbkdf2_hmac_sha256", "iterations": iters,
                "salt": base64.b64encode(salt).decode(),
                "hash": base64.b64encode(
                    _hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                         salt, iters)).decode()}

    good = cheap("hunter2")
    check("the right password verifies", utils.verify_password("hunter2", good) is True)
    check("a wrong one (even case-changed) doesn't",
          utils.verify_password("Hunter2", good) is False)
    check("an empty password is refused before any hashing happens",
          utils.verify_password("", good) is False)
    check("no record at all is False, not an exception",
          utils.verify_password("hunter2", None) is False)
    check("a record from another kdf is refused, not guessed at",
          utils.verify_password("hunter2", {**good, "kdf": "sha256"}) is False)
    check("a corrupt salt fails closed",
          utils.verify_password("hunter2", {**good, "salt": "!!not-base64!!"}) is False)
    check("a record missing its salt fails closed",
          utils.verify_password("hunter2", {"kdf": "pbkdf2_hmac_sha256"}) is False)
    check("the stored iteration count is what's used",
          utils.verify_password("hunter2", cheap("hunter2", iters=7)) is True)
    check("…so a record claiming a different count doesn't match",
          utils.verify_password("hunter2", {**good, "iterations": 999}) is False)

    fp = utils.lock_fingerprint(good)
    check("the fingerprint is stable for a given record",
          fp == utils.lock_fingerprint(cheap("hunter2")))
    check("…and changes when the salt does, so re-locking rotates it",
          fp != utils.lock_fingerprint(cheap("hunter2", salt=b"fedcba9876543210")))
    check("…ditto the iteration count",
          fp != utils.lock_fingerprint(cheap("hunter2", iters=2000)))
    check("no lock gives an empty fingerprint, not None",
          utils.lock_fingerprint(None) == "" and utils.lock_fingerprint({}) == "")

    # ── set_file_lock ────────────────────────────────────────────────────────
    print("\nset_file_lock:")
    db7 = Database()
    await db7.save_file("v", {"file_name": "V.mkv", "type": "video",
                              "media": manifest([{"index": 0, "label": "English"}])})
    check("a file that doesn't exist can't be locked",
          await db7.set_file_lock("nope", good) is False)
    check("locking returns True", await db7.set_file_lock("v", good) is True)
    m = await db7.get_file("v")
    check("the lock record is stored on the file", m["lock"]["kdf"] == "pbkdf2_hmac_sha256")
    check("…without disturbing the media manifest, which the FFmpeg job writes later",
          m["media"]["subtitles"][0]["label"] == "English")
    check("…or any other field", m["file_name"] == "V.mkv" and m["type"] == "video")
    db8 = Database()
    db8._load_local()
    check("the lock survives a restart through the JSON snapshot",
          db8._files["v"]["lock"]["salt"] == good["salt"])
    check("unlocking returns True", await db7.set_file_lock("v", None) is True)
    check("…and drops the key outright rather than storing a null",
          "lock" not in await db7.get_file("v"))
    check("unlocking an already-unlocked file still succeeds (the row is there)",
          await db7.set_file_lock("v", None) is True)
    check("…while a missing row is False either way",
          await db7.set_file_lock("ghost", None) is False)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
