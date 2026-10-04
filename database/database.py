"""
Database layer — Motor (async MongoDB) when MONGO_URI is set, else in-memory.
Includes: users, files, batch groups, stats counters, active sessions tracking.

BUG FIX (download/stream 404 after a restart):
When no MONGO_URI is configured the store lives only in RAM, so every link
generated before a restart/redeploy returns 404 ("File Not Found"). The
in-memory backend now persists itself to a small JSON file on disk and reloads
it on startup, so links survive restarts as long as the filesystem persists.
"""

import asyncio
import contextlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

import info

logger = logging.getLogger(__name__)
LOCAL_DB_PATH: str = os.environ.get("LOCAL_DB_PATH", "local_db.json").strip() or "local_db.json"

# How long a hot-path mutation waits before hitting disk, and how much
# active-session history is worth keeping.
_SAVE_DEBOUNCE_SECONDS = 5
_ACTIVE_SESSIONS_MAX = 5000
_ACTIVE_SESSIONS_RETENTION_DAYS = 30


class Database:

    def __init__(self):
        self._backend = "memory"
        self._users: Dict[int, dict] = {}
        self._files: Dict[str, dict] = {}
        self._batches: Dict[str, dict] = {}
        # Guards concurrent disk writes now that _save_local_async() runs the
        # write in a worker thread: with the write off the event loop, two
        # increment_file_stat() calls firing at once (e.g. two viewers
        # loading a file page in the same instant) can genuinely run
        # _save_local() in parallel on separate OS threads. Both would build
        # LOCAL_DB_PATH + ".tmp" and os.replace() it into place, and one
        # thread's rename can beat the other to a tmp file the other already
        # moved, raising FileNotFoundError. The lock serializes the writes
        # (they're cheap and infrequent-ish) while keeping them off the
        # event loop.
        self._save_lock = asyncio.Lock()
        self._stats: Dict[str, int] = {"links_generated": 0, "files_uploaded": 0, "streams_served": 0, "downloads_served": 0}
        self._active_sessions: Dict[int, datetime] = {}  # user_id → last_seen
        # Debounced-save state (see _mark_dirty / flush).
        self._dirty = False
        self._flush_handle: "asyncio.TimerHandle | None" = None
        self._flush_task: "asyncio.Task | None" = None
        self._db = None

    # ── Connect ───────────────────────────────────────────────────────────────

    # Fields stored as datetime objects that must be (de)serialized for JSON.
    _DT_FIELDS = ("saved_at", "expires_at", "joined", "last_seen", "created_at")

    def _load_local(self):
        """Reload the in-memory store from disk (if a snapshot exists)."""
        if not os.path.exists(LOCAL_DB_PATH):
            return
        try:
            with open(LOCAL_DB_PATH, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except Exception as e:
            logger.warning("Could not read local DB snapshot (%s): %s", LOCAL_DB_PATH, e)
            return

        def _revive(doc: dict) -> dict:
            # Every datetime in this store is UTC and compared against
            # datetime.utcnow(), which is naive. An ISO string that carries an
            # offset ("…+05:30", perfectly legal to write) parses to a
            # tz-AWARE datetime, and aware <naive> raises TypeError — which in
            # delete_expired_files() would abort the whole sweep, leaving every
            # expired link in place. Normalizing here means the rest of this
            # class can compare dates without each call site re-guarding.
            for k in self._DT_FIELDS:
                v = doc.get(k)
                if isinstance(v, str):
                    try:
                        v = datetime.fromisoformat(v)
                    except Exception:
                        continue
                if isinstance(v, datetime) and v.tzinfo is not None:
                    v = v.astimezone(timezone.utc).replace(tzinfo=None)
                doc[k] = v
            return doc

        self._files   = {k: _revive(v) for k, v in snap.get("files", {}).items()}
        self._batches = {k: _revive(v) for k, v in snap.get("batches", {}).items()}
        self._users   = {int(k): _revive(v) for k, v in snap.get("users", {}).items()}
        # Restore the active-session map too: without this, /stats and the
        # "active users" numbers read 0 after every restart on the memory
        # backend while MongoDB (which stores last_seen on the user doc)
        # reported real values — same command, two different answers.
        sessions = {}
        for k, v in snap.get("sessions", {}).items():
            try:
                sessions[int(k)] = datetime.fromisoformat(v)
            except (TypeError, ValueError):
                continue
        self._active_sessions = sessions
        if isinstance(snap.get("stats"), dict):
            self._stats.update(snap["stats"])
        logger.info(
            "Loaded local DB snapshot: %d files, %d batches, %d users.",
            len(self._files), len(self._batches), len(self._users),
        )

    def _build_snapshot(self) -> dict:
        """Serialize the live store into a plain dict.

        MUST run on the event loop thread, never in a worker thread: the
        dicts here are being mutated concurrently by other coroutines, and
        iterating them from another thread can raise
        "dictionary changed size during iteration" mid-comprehension. That
        error used to escape _save_local()'s try block (which only wrapped
        the file write), silently skipping the save entirely — i.e. the
        durable state we're trying to persist was lost exactly under the
        concurrent traffic this path exists to handle.
        """
        def _plain(doc: dict) -> dict:
            out = {}
            for k, v in doc.items():
                out[k] = v.isoformat() if isinstance(v, datetime) else v
            return out

        return {
            "files":   {k: _plain(v) for k, v in self._files.items()},
            "batches": {k: _plain(v) for k, v in self._batches.items()},
            "users":   {str(k): _plain(v) for k, v in self._users.items()},
            "stats":   dict(self._stats),
            "sessions": {str(k): v.isoformat() for k, v in self._active_sessions.items()},
        }

    @staticmethod
    def _write_snapshot(snap: dict) -> None:
        """Write an already-serialized snapshot to disk (blocking — call
        from a worker thread via asyncio.to_thread, never on the loop).
        Callers hold _save_lock, so the shared .tmp name can't collide."""
        try:
            tmp = LOCAL_DB_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, LOCAL_DB_PATH)   # atomic write — never a half file
        except Exception as e:
            logger.warning("Could not persist local DB snapshot: %s", e)

    def _save_local(self):
        """Persist the in-memory store to disk. No-op for the Mongo backend."""
        if self._backend != "memory":
            return
        self._write_snapshot(self._build_snapshot())

    async def _save_local_async(self):
        """
        Persist the in-memory store to disk WITHOUT blocking the event loop.

        BUG FIX — concurrent download/stream blocking:
        _save_local() does synchronous file I/O: open() + json.dump() of the
        entire in-memory store (every file, batch, user, and stat) followed
        by os.replace(). It used to be invoked directly (as a plain blocking
        call) from async methods such as increment_file_stat(), which fires
        on every single /stream and /download open and every /file page
        view. aiohttp's event loop is single-threaded, so that synchronous
        write stalled every OTHER in-flight request — every other stream's
        chunk writes, every other page load, even /health — for as long as
        the write took. And the write gets bigger and slower as the store
        grows, so the stall gets worse over time under exactly the kind of
        concurrent traffic this platform is meant to serve. Running the
        write in a worker thread keeps the event loop free to keep pumping
        concurrent downloads while the write happens in the background.
        """
        if self._backend != "memory":
            return
        async with self._save_lock:
            snap = self._build_snapshot()          # on the loop: consistent view
            await asyncio.to_thread(self._write_snapshot, snap)   # off the loop: blocking I/O

    def _mark_dirty(self):
        """Schedule a debounced save for a hot-path mutation.

        Counter bumps (view/stream/download counts, per-file stats) happen on
        every single request. Writing the WHOLE store to disk for each one is
        both wasteful and, before the threaded save existed, a stall — and
        skipping the write entirely (the old behaviour for increment_stat)
        lost the counts on restart. Coalescing bursts into one write per
        _SAVE_DEBOUNCE_SECONDS keeps the counts durable without the per-hit
        cost, since nothing outside this process reads these counters in
        real time.
        """
        if self._backend != "memory":
            return
        self._dirty = True
        if self._flush_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_handle = None
            return
        self._flush_handle = loop.call_later(_SAVE_DEBOUNCE_SECONDS, self._schedule_flush)

    def _schedule_flush(self):
        self._flush_handle = None
        try:
            self._flush_task = asyncio.create_task(self.flush())
        except RuntimeError:
            pass

    async def flush(self):
        """Write any pending debounced changes to disk now.

        Awaited before shutdown and before the setup wizard's os.execv
        restart, so counters accumulated since the last save aren't lost.
        """
        if self._backend != "memory" or not self._dirty:
            return
        self._dirty = False
        await self._save_local_async()

    async def flush_safe(self):
        """flush() that never raises — for shutdown paths and fire-and-forget
        calls where a persistence failure must not mask the real work."""
        with contextlib.suppress(Exception):
            await self.flush()

    async def connect(self):
        mongo_uri = info.MONGO_URI
        if not mongo_uri:
            logger.info(
                "No MONGO_URI configured (set it via Telegram with /setup or "
                "/settings) — using in-memory store with disk persistence at '%s'.",
                LOCAL_DB_PATH,
            )
            self._load_local()
            return
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
            client = AsyncIOMotorClient(mongo_uri, serverSelectionTimeoutMS=5000)
            await client.admin.command("ping")
            self._db = client["file_to_link_bot"]
            self._backend = "mongo"
            await self._ensure_indexes()
            logger.info("MongoDB connected ✅")
        except ImportError:
            logger.warning("motor not installed — using in-memory store.")
        except Exception as e:
            logger.warning("MongoDB connection failed (%s) — using in-memory store.", e)

    async def _ensure_indexes(self):
        """Create indexes that back the admin/stats queries.

        Without these, /stats, /status, get_recent_files, get_today_users
        etc. trigger full collection scans that get progressively slower as
        the bot grows. create_index is idempotent, so this is safe to run on
        every startup.
        """
        try:
            await self._db["users"].create_index("last_seen")
            await self._db["users"].create_index("joined")
            await self._db["users"].create_index("banned")
            await self._db["files"].create_index("saved_at")
            # get_user_file_stats() aggregates on this ("My Stats" per user).
            # Without an index it was a full files-collection scan per tap.
            await self._db["files"].create_index("uploader_id")
            await self._db["batches"].create_index([("creator_id", 1), ("status", 1)])
        except Exception as e:
            logger.warning("Could not create indexes (non-fatal): %s", e)

    # ── Active Sessions ───────────────────────────────────────────────────────

    async def mark_active(self, user_id: int):
        """Mark user as active (last seen now)."""
        now = datetime.utcnow()
        self._active_sessions[user_id] = now
        # Bound the map: it's keyed by every user who has ever messaged the
        # bot and was previously never pruned, so it only ever grew. Trimming
        # anything older than the retention window keeps it proportional to
        # genuinely active users instead of total historical signups.
        if len(self._active_sessions) > _ACTIVE_SESSIONS_MAX:
            cutoff = now - timedelta(days=_ACTIVE_SESSIONS_RETENTION_DAYS)
            for uid in [u for u, ts in self._active_sessions.items() if ts < cutoff]:
                self._active_sessions.pop(uid, None)
        if self._backend == "mongo":
            await self._db["users"].update_one(
                {"_id": user_id},
                {"$set": {"last_seen": now}},
            )
        else:
            # Mirror onto the user record too. The memory backend previously
            # only wrote the session map, so get_user_info()/userinfo's
            # "last seen" froze at signup there while Mongo kept it fresh.
            user = self._users.get(user_id)
            if user is not None:
                user["last_seen"] = now

    async def active_users_count(self, minutes: int = 30) -> int:
        """Count users active in last N minutes."""
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        if self._backend == "mongo":
            return await self._db["users"].count_documents({"last_seen": {"$gte": cutoff}})
        return sum(1 for ts in self._active_sessions.values() if ts >= cutoff)

    async def active_users_today(self) -> int:
        cutoff = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        if self._backend == "mongo":
            return await self._db["users"].count_documents({"last_seen": {"$gte": cutoff}})
        return sum(1 for ts in self._active_sessions.values() if ts >= cutoff)

    # ── Stats Counters ────────────────────────────────────────────────────────

    async def increment_stat(self, key: str, amount: int = 1):
        self._stats[key] = self._stats.get(key, 0) + amount
        if self._backend == "mongo":
            await self._db["stats"].update_one(
                {"_id": "global"},
                {"$inc": {key: amount}},
                upsert=True,
            )
        else:
            # BUG FIX: this used to persist nothing at all, so global counters
            # only reached disk whenever some *other* call happened to save.
            # A restart mid-quiet-period silently reset "links generated" /
            # "streams served" to whatever the last unrelated write captured.
            self._mark_dirty()

    async def get_stats(self) -> dict:
        if self._backend == "mongo":
            doc = await self._db["stats"].find_one({"_id": "global"}) or {}
            return {k: doc.get(k, 0) for k in ("links_generated", "files_uploaded", "streams_served", "downloads_served")}
        return dict(self._stats)

    async def get_today_links(self) -> int:
        """Count files saved today."""
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        if self._backend == "mongo":
            return await self._db["files"].count_documents({"saved_at": {"$gte": today}})
        return sum(1 for f in self._files.values() if f.get("saved_at", datetime.min) >= today)

    async def get_today_users(self) -> int:
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        if self._backend == "mongo":
            return await self._db["users"].count_documents({"joined": {"$gte": today}})
        return sum(1 for u in self._users.values() if u.get("joined", datetime.min) >= today)

    # ── Users ─────────────────────────────────────────────────────────────────

    async def add_user(self, user_id: int, name: str = "", username: str = "") -> bool:
        if self._backend == "mongo":
            col = self._db["users"]
            # One atomic upsert instead of find_one-then-insert: two
            # simultaneous first messages used to both see "not existing"
            # and the loser died on an uncaught DuplicateKeyError.
            r = await col.update_one(
                {"_id": user_id},
                {"$setOnInsert": {
                    "name": name, "username": username,
                    "joined": datetime.utcnow(), "banned": False,
                    "last_seen": datetime.utcnow(),
                }},
                upsert=True,
            )
            is_new = r.upserted_id is not None or r.matched_count == 0
            if not is_new:
                # Keep stored name/username fresh in case they changed them.
                await col.update_one(
                    {"_id": user_id},
                    {"$set": {"name": name, "username": username,
                              "last_seen": datetime.utcnow()}},
                )
            return is_new
        if user_id not in self._users:
            self._users[user_id] = {
                "name": name, "username": username,
                "joined": datetime.utcnow(), "banned": False,
                "last_seen": datetime.utcnow(),
            }
            # BUG FIX (local-backend data loss): new-user creation carries
            # durable state (joined date, banned flag) that shouldn't only
            # live in memory — save immediately so it survives a restart.
            # Deliberately NOT saving on the "just refresh name/username"
            # branch below: add_user() fires on EVERY plain-text message
            # (see plugins/auto_help.py), so an unconditional save here
            # would re-introduce the exact "full-store write stalls every
            # other in-flight request" problem _save_local_async()'s own
            # docstring already warns about. A cosmetic name/username
            # refresh is fine to stay best-effort and ride along on the
            # next save triggered by something else.
            await self._save_local_async()
            return True
        # Keep stored name/username fresh in case the user changed them.
        self._users[user_id]["name"] = name
        self._users[user_id]["username"] = username
        return False

    async def is_user_exist(self, user_id: int) -> bool:
        if self._backend == "mongo":
            return bool(await self._db["users"].find_one({"_id": user_id}))
        return user_id in self._users

    async def total_users_count(self) -> int:
        if self._backend == "mongo":
            return await self._db["users"].count_documents({})
        return len(self._users)

    async def get_all_users(self):
        if self._backend == "mongo":
            async for user in self._db["users"].find({}):
                yield user
        else:
            # Snapshot first. This is an async generator and every caller
            # awaits inside the loop (broadcasts send one message per user),
            # so yielding straight off the live dict let a concurrent
            # add_user() resize it mid-iteration and raise
            # "dictionary changed size during iteration" partway through a
            # broadcast. Copy the docs too, not just the key list — a later
            # **data unpack of a still-live dict can hit the same race.
            docs = [{"_id": uid, **data} for uid, data in self._users.items()]
            for doc in docs:
                yield doc

    async def ban_user(self, user_id: int) -> bool:
        if self._backend == "mongo":
            r = await self._db["users"].update_one({"_id": user_id}, {"$set": {"banned": True}})
            return r.modified_count > 0
        if user_id in self._users:
            self._users[user_id]["banned"] = True
            # BUG FIX (local-backend data loss): admin actions like this are
            # rare enough (only fires from /ban) that an immediate save is
            # cheap, and important enough (a ban silently not persisting is
            # a real moderation gap) that it shouldn't be left to chance on
            # some unrelated future save happening to flush it first.
            await self._save_local_async()
            return True
        return False

    async def unban_user(self, user_id: int) -> bool:
        if self._backend == "mongo":
            r = await self._db["users"].update_one({"_id": user_id}, {"$set": {"banned": False}})
            return r.modified_count > 0
        if user_id in self._users:
            self._users[user_id]["banned"] = False
            await self._save_local_async()  # see ban_user's comment above
            return True
        return False

    async def is_banned(self, user_id: int) -> bool:
        if self._backend == "mongo":
            doc = await self._db["users"].find_one({"_id": user_id})
            return doc.get("banned", False) if doc else False
        return self._users.get(user_id, {}).get("banned", False)

    async def get_banned_count(self) -> int:
        if self._backend == "mongo":
            return await self._db["users"].count_documents({"banned": True})
        return sum(1 for u in self._users.values() if u.get("banned"))

    # ── Anti-spam: warnings & temporary bans ─────────────────────────────────

    async def _ensure_user(self, user_id: int):
        """Make sure a user document exists (for users who message the bot
        without ever sending /start, e.g. via deep links)."""
        if self._backend == "mongo":
            await self._db["users"].update_one(
                {"_id": user_id},
                {"$setOnInsert": {
                    "name": "", "username": "", "joined": datetime.utcnow(),
                    "banned": False, "last_seen": datetime.utcnow(),
                    "warnings": 0, "temp_ban_until": None,
                }},
                upsert=True,
            )
        else:
            if user_id not in self._users:
                self._users[user_id] = {
                    "name": "", "username": "", "joined": datetime.utcnow(),
                    "banned": False, "last_seen": datetime.utcnow(),
                    "warnings": 0, "temp_ban_until": None,
                }

    async def increment_warning(self, user_id: int) -> int:
        """Increment a user's spam-warning count and return the new total."""
        await self._ensure_user(user_id)
        if self._backend == "mongo":
            await self._db["users"].update_one(
                {"_id": user_id}, {"$inc": {"warnings": 1}}, upsert=True
            )
            doc = await self._db["users"].find_one({"_id": user_id})
            return doc.get("warnings", 1) if doc else 1
        u = self._users[user_id]
        u["warnings"] = u.get("warnings", 0) + 1
        # BUG FIX (local-backend data loss): this is gated by antispam's own
        # cooldown (at most once per ~8s per flagged user), so it's cheap
        # enough to persist immediately rather than risk a warning count
        # silently resetting to 0 on a restart before anything else saves.
        await self._save_local_async()
        return u["warnings"]

    async def get_warnings(self, user_id: int) -> int:
        if self._backend == "mongo":
            doc = await self._db["users"].find_one({"_id": user_id})
            return doc.get("warnings", 0) if doc else 0
        return self._users.get(user_id, {}).get("warnings", 0)

    async def reset_warnings(self, user_id: int):
        if self._backend == "mongo":
            await self._db["users"].update_one({"_id": user_id}, {"$set": {"warnings": 0}})
        elif user_id in self._users:
            self._users[user_id]["warnings"] = 0
            await self._save_local_async()  # see increment_warning's comment above

    async def set_temp_ban(self, user_id: int, until: datetime):
        await self._ensure_user(user_id)
        if self._backend == "mongo":
            await self._db["users"].update_one(
                {"_id": user_id}, {"$set": {"temp_ban_until": until}}
            )
        else:
            self._users[user_id]["temp_ban_until"] = until
            await self._save_local_async()  # see increment_warning's comment above

    async def get_temp_ban(self, user_id: int) -> Optional[datetime]:
        if self._backend == "mongo":
            doc = await self._db["users"].find_one({"_id": user_id})
            until = doc.get("temp_ban_until") if doc else None
        else:
            until = self._users.get(user_id, {}).get("temp_ban_until")

        if isinstance(until, str):
            try:
                until = datetime.fromisoformat(until)
            except Exception:
                until = None
        return until

    async def clear_temp_ban(self, user_id: int):
        if self._backend == "mongo":
            await self._db["users"].update_one(
                {"_id": user_id}, {"$set": {"temp_ban_until": None}}
            )
        elif user_id in self._users:
            self._users[user_id]["temp_ban_until"] = None
            # BUG FIX (permanent lockout on the memory backend): set_temp_ban
            # saved immediately but this did not, so a lift could live only in
            # RAM. Restart before the next unrelated save and the stale ban
            # expiry came back off disk — the user stayed blocked out even
            # though an admin had explicitly cleared it.
            await self._save_local_async()

    async def set_privacy_mode(self, user_id: int, enabled: bool) -> None:
        """
        Toggle whether this user's identity is shown in the "Uploaded by"
        caption block (see utils.build_uploader_caption). When enabled,
        their real name/username/ID are still stored here as normal —
        this only controls what's DISPLAYED publicly in file captions.
        Admin-facing tools (e.g. /userinfo) always read the real stored
        values regardless of this flag; only the caption text respects it.
        """
        await self._ensure_user(user_id)
        if self._backend == "mongo":
            await self._db["users"].update_one(
                {"_id": user_id}, {"$set": {"privacy_mode": enabled}}
            )
        else:
            self._users[user_id]["privacy_mode"] = enabled
            # User-initiated, low-frequency (a settings toggle, not
            # something that fires per-message) — safe and worth
            # persisting immediately. See increment_warning's comment
            # above for why this pattern isn't used everywhere.
            await self._save_local_async()

    async def set_timezone(self, user_id: int, tz_name: "str | None") -> None:
        """
        Store this user's preferred IANA timezone name (e.g. "Asia/Kolkata"),
        or None to reset to the default (UTC). Used by utils.to_user_local_time
        to display dates/times in each person's own timezone instead of
        always UTC. See plugins/timezone.py for the /timezone command.
        """
        await self._ensure_user(user_id)
        if self._backend == "mongo":
            await self._db["users"].update_one(
                {"_id": user_id}, {"$set": {"timezone": tz_name}}
            )
        else:
            self._users[user_id]["timezone"] = tz_name
            # User-initiated, low-frequency (a settings command, not
            # something that fires per-message) — safe and worth
            # persisting immediately. See increment_warning's comment
            # for why this pattern isn't used everywhere.
            await self._save_local_async()

    async def get_user_info(self, user_id: int) -> Optional[dict]:
        if self._backend == "mongo":
            return await self._db["users"].find_one({"_id": user_id})
        u = self._users.get(user_id)
        return {"_id": user_id, **u} if u else None

    async def get_recent_users(self, limit: int = 10) -> list:
        """Most recently joined users (with name/username) for admin panels."""
        if self._backend == "mongo":
            cursor = self._db["users"].find({}).sort("joined", -1).limit(limit)
            return await cursor.to_list(length=limit)
        sorted_users = sorted(
            self._users.items(), key=lambda x: x[1].get("joined", datetime.min), reverse=True
        )
        return [{"_id": k, **v} for k, v in sorted_users[:limit]]

    async def get_users_since(self, cutoff: datetime) -> list:
        """Users who joined on/after `cutoff`.

        Exists so /analytics doesn't have to stream the entire user
        collection through Python just to filter recent signups — Mongo does
        it with the existing `joined` index, and the memory backend avoids
        materializing every user dict.
        """
        if self._backend == "mongo":
            cursor = self._db["users"].find({"joined": {"$gte": cutoff}})
            return await cursor.to_list(length=None)
        return [
            {"_id": uid, **data} for uid, data in self._users.items()
            if isinstance(data.get("joined"), datetime) and data["joined"] >= cutoff
        ]

    async def delete_all_files(self) -> int:
        """Delete every stored file record. Returns how many were removed.

        Admins used to reach into db._files.clear() directly, which only
        touched RAM on the memory backend — the on-disk snapshot kept every
        record, so a "deleted" library simply reappeared after a restart.
        """
        if self._backend == "mongo":
            r = await self._db["files"].delete_many({})
            return r.deleted_count
        count = len(self._files)
        self._files.clear()
        await self._save_local_async()
        return count

    # ── Files ─────────────────────────────────────────────────────────────────

    async def save_file(self, file_id: str, meta: dict) -> None:
        data = {**meta, "saved_at": datetime.utcnow()}
        if self._backend == "mongo":
            await self._db["files"].update_one(
                {"_id": file_id},
                {"$set": data},
                upsert=True,
            )
        else:
            self._files[file_id] = data
            await self._save_local_async()

    async def get_file(self, file_id: str) -> Optional[Dict[str, Any]]:
        if self._backend == "mongo":
            return await self._db["files"].find_one({"_id": file_id})
        return self._files.get(file_id)

    async def increment_file_stat(self, file_id: str, key: str, amount: int = 1) -> None:
        """Bump a per-file counter (view_count / stream_count / dl_count).

        Powers the popularity numbers shown on each file page. Cheap and
        best-effort — failures here must never break a download/stream.
        """
        if self._backend == "mongo":
            await self._db["files"].update_one(
                {"_id": file_id}, {"$inc": {key: amount}}
            )
            return
        f = self._files.get(file_id)
        if f is None:
            return
        f[key] = int(f.get(key, 0)) + amount
        # Debounced rather than an immediate full-store write: this fires on
        # every page view and every stream/download open, so saving inline
        # serialized a JSON dump of the whole store per hit.
        self._mark_dirty()

    async def set_file_media(self, file_id: str, manifest: dict) -> None:
        """Attach the extracted subtitle/audio track manifest to a file.

        Written by web/media.py once FFmpeg has finished probing a video,
        which can be minutes after the link was handed to the user — so this
        has to be a standalone update rather than part of save_file().
        """
        if self._backend == "mongo":
            await self._db["files"].update_one(
                {"_id": file_id}, {"$set": {"media": manifest}}
            )
            return
        f = self._files.get(file_id)
        if f is None:
            return
        f["media"] = manifest
        await self._save_local_async()

    # ── Manual subtitle association (the /sub command) ────────────────────────
    #
    # A subtitle is stored as a normal file record of its own (its own
    # file_uid, uploaded through the regular path) and merely *referenced*
    # from the target video's manifest — exactly how a batch sidecar works,
    # except the link is explicit and stored rather than guessed from the
    # filename at render time. web/app.py's subtitle_handler already serves
    # any manifest entry that carries a `src_uid` by downloading and
    # converting it on first hit, so nothing new is needed on the serving
    # side. Indices live in a dedicated 200-255 band: FFmpeg-extracted
    # streams use 0-99 and batch-discovered sidecars use 100-199
    # (see _collect_sidecar_subtitles), so an attached subtitle can never
    # collide with either.
    _ATTACHED_SUB_BASE = 200

    async def attach_subtitle(self, file_id: str, sub_uid: str,
                              label: str = "Subtitles", language: str = "",
                              default: bool = False) -> "int | None":
        """Refer `sub_uid` as a subtitle track on `file_id`. Returns the
        assigned track index, or None if the target file doesn't exist."""
        target = await self.get_file(file_id)
        if not target:
            return None
        manifest = dict(target.get("media") or {})
        subs = list(manifest.get("subtitles") or [])
        used = {s.get("index") for s in subs}
        idx = self._ATTACHED_SUB_BASE
        while idx in used and idx < 256:
            idx += 1
        if idx >= 256:
            return None
        subs.append({
            "index": idx,
            "label": label or "Subtitles",
            "language": language or "",
            "default": bool(default),
            "src_uid": sub_uid,
        })
        manifest["subtitles"] = subs
        await self.set_file_media(file_id, manifest)
        return idx

    def attached_subtitles(self, file_meta: dict) -> list:
        """src_uids of subtitle files added to this record by /sub.

        Only the 200+ band counts: a batch sidecar is described at page-render
        time and is a full batch member in its own right, so deleting a video
        must not sweep up the unrelated .srt that happened to be uploaded next
        to it.

        `or {}` rather than a `.get()` default on the way in: a record whose
        media field is explicitly null (an old row, a hand-edited snapshot)
        still returns the key, so the default never fires and the follow-up
        .get() lands on None. This runs inside /delete and /unlink, where one
        AttributeError means the whole delete aborts after the DB row is gone.
        """
        return [s.get("src_uid")
                for s in ((file_meta or {}).get("media") or {}).get("subtitles", [])
                if s.get("src_uid")
                and (s.get("index") or 0) >= self._ATTACHED_SUB_BASE]

    async def detach_subtitle(self, file_id: str, index: int) -> tuple:
        """
        Drop the attached subtitle at `index`; returns (removed, src_uid).

        Refuses anything outside the /sub band on purpose. An FFmpeg-extracted
        track (0-99) is part of the video itself, so deleting its manifest
        entry would only hide it until the next re-probe put it straight back —
        while its remuxed audio stayed on disk. A batch sidecar (100-199) is a
        full batch member that /unsub has no business removing. `removed` is
        False when no detachable track has that index; `src_uid` may still be
        None for a manifest entry written without one.
        """
        target = await self.get_file(file_id)
        if not target:
            return False, None
        manifest = dict(target.get("media") or {})
        subs = list(manifest.get("subtitles") or [])
        keep, removed, src_uid = [], False, None
        for s in subs:
            if (not removed and s.get("index") == index
                    and (s.get("index") or 0) >= self._ATTACHED_SUB_BASE):
                removed, src_uid = True, s.get("src_uid")
                continue
            keep.append(s)
        if not removed:
            return False, None
        manifest["subtitles"] = keep
        await self.set_file_media(file_id, manifest)
        return True, src_uid

    async def set_file_lock(self, file_id: str, lock: Optional[dict]) -> bool:
        """Store (or, with None, remove) the password record on a file.

        Single-field $set for the same reason rename_file() uses one: the
        media manifest and the rest of the record must not be touched, and a
        read-modify-write of the whole document would race with the FFmpeg
        track job that writes `media` minutes after the upload.
        """
        if self._backend == "mongo":
            r = await self._db["files"].update_one(
                {"_id": file_id}, {"$set": {"lock": lock}}
            )
            return r.matched_count > 0
        f = self._files.get(file_id)
        if f is None:
            return False
        if lock is None:
            f.pop("lock", None)
        else:
            f["lock"] = lock
        await self._save_local_async()
        return True

    async def rename_file(self, file_id: str, new_name: str) -> bool:
        """Change a stored file's display/download name. Uses $set on a
        single field so every other field (including the media manifest) is
        left untouched."""
        if self._backend == "mongo":
            r = await self._db["files"].update_one(
                {"_id": file_id}, {"$set": {"file_name": new_name}}
            )
            return r.matched_count > 0
        f = self._files.get(file_id)
        if f is None:
            return False
        f["file_name"] = new_name
        await self._save_local_async()
        return True

    async def get_files_by_uploader(self, user_id: int, limit: int = 10) -> list:
        """Most-recent files a single user uploaded, newest first. Backs the
        /mylinks command — until now the only file listing was the admin-only
        /recentfiles, so a user who lost a link had no way to find it again.
        `uploader_id` is already indexed (see connect()), so this is a cheap
        indexed lookup rather than a collection scan."""
        if self._backend == "mongo":
            cursor = (self._db["files"]
                      .find({"uploader_id": user_id})
                      .sort("saved_at", -1)
                      .limit(limit))
            return await cursor.to_list(length=limit)
        mine = [(k, v) for k, v in self._files.items()
                if v.get("uploader_id") == user_id]
        mine.sort(key=lambda kv: kv[1].get("saved_at") or datetime.min, reverse=True)
        return [{"_id": k, **v} for k, v in mine[:limit]]

    async def total_files_count(self) -> int:
        if self._backend == "mongo":
            return await self._db["files"].count_documents({})
        return len(self._files)

    async def delete_file(self, file_id: str) -> bool:
        if self._backend == "mongo":
            r = await self._db["files"].delete_one({"_id": file_id})
            return r.deleted_count > 0
        removed = bool(self._files.pop(file_id, None))
        if removed:
            await self._save_local_async()
        return removed

    async def delete_expired_files(self, now: Optional[datetime] = None) -> int:
        """
        Drop every file record whose link has expired; returns how many.

        Until now expiry was purely cosmetic: the web page answered 410 and
        the bot's /start lookup refused the link, but the record itself —
        plus its extracted tracks on disk — stayed in the database forever.
        A deployment with LINK_EXPIRY_DAYS set therefore accumulated every
        expired upload permanently, which is the opposite of what an expiry
        setting is for.

        Runs on the same hourly tick as web/media.py's orphan track sweep,
        and BEFORE it, so tracks belonging to files deleted here are cleaned
        up in the very same pass.
        """
        now = now or datetime.utcnow()
        if self._backend == "mongo":
            col = self._db["files"]
            deleted = 0
            # Two queries, not one $or: records written from here on store a
            # real BSON date, but rows saved before that stored an ISO
            # string, and Mongo never compares across those types — a single
            # date query would leave every pre-existing string row behind
            # permanently, silently, with no error to notice by.
            for cutoff in (now, now.isoformat()):
                r = await col.delete_many({"expires_at": {"$lte": cutoff}})
                deleted += r.deleted_count
            return deleted
        expired = [
            fid for fid, data in self._files.items()
            if isinstance(data.get("expires_at"), datetime) and data["expires_at"] <= now
        ]
        if not expired:
            return 0
        for fid in expired:
            self._files.pop(fid, None)
        await self._save_local_async()
        return len(expired)

    async def get_recent_files(self, limit: int = 5) -> list:
        if self._backend == "mongo":
            cursor = self._db["files"].find({}).sort("saved_at", -1).limit(limit)
            return await cursor.to_list(length=limit)
        sorted_files = sorted(self._files.items(), key=lambda x: x[1].get("saved_at", datetime.min), reverse=True)
        return [{"_id": k, **v} for k, v in sorted_files[:limit]]

    async def get_files_since(self, cutoff: datetime) -> list:
        """
        All files saved on/after `cutoff`, unordered. Unlike get_recent_files
        (which is capped by count), this is capped by date range — used for
        the /analytics chart, which needs every upload in a window (e.g. the
        last 14 days) rather than just the N most recent overall.
        """
        if self._backend == "mongo":
            cursor = self._db["files"].find({"saved_at": {"$gte": cutoff}})
            return await cursor.to_list(length=None)
        out = []
        for k, v in self._files.items():
            saved_at = v.get("saved_at")
            # Defensive: local storage round-trips datetimes through
            # isoformat()/fromisoformat() on save/load (see _load_local),
            # so this should already be a real datetime — but guard against
            # a stray string the same way plugins/admin.py does elsewhere.
            if isinstance(saved_at, str):
                try:
                    saved_at = datetime.fromisoformat(saved_at)
                except Exception:
                    continue
            if isinstance(saved_at, datetime) and saved_at >= cutoff:
                out.append({"_id": k, **v})
        return out

    async def get_user_file_stats(self, user_id: int) -> dict:
        """
        Per-user upload totals — file count, combined size, and combined
        view/stream/download counts across every file this user uploaded.

        BUG FIX: the "📊 My Stats" button (plugins/start.py) computed
        user_id and then never used it, showing the same bot-wide numbers
        to every user regardless of who tapped it. This is what actually
        powers a personalized reply instead.
        """
        if self._backend == "mongo":
            cursor = self._db["files"].aggregate([
                {"$match": {"uploader_id": user_id}},
                {"$group": {
                    "_id": None,
                    "file_count": {"$sum": 1},
                    "total_size": {"$sum": {"$ifNull": ["$file_size", 0]}},
                    "views": {"$sum": {"$ifNull": ["$view_count", 0]}},
                    "streams": {"$sum": {"$ifNull": ["$stream_count", 0]}},
                    "downloads": {"$sum": {"$ifNull": ["$dl_count", 0]}},
                }},
            ])
            docs = await cursor.to_list(length=1)
            if not docs:
                return {"file_count": 0, "total_size": 0, "views": 0, "streams": 0, "downloads": 0}
            doc = docs[0]
            doc.pop("_id", None)
            return doc

        file_count = total_size = views = streams = downloads = 0
        for f in self._files.values():
            if f.get("uploader_id") != user_id:
                continue
            file_count += 1
            total_size += int(f.get("file_size") or 0)
            views += int(f.get("view_count") or 0)
            streams += int(f.get("stream_count") or 0)
            downloads += int(f.get("dl_count") or 0)
        return {
            "file_count": file_count, "total_size": total_size,
            "views": views, "streams": streams, "downloads": downloads,
        }

    # ── Batches ───────────────────────────────────────────────────────────────

    async def create_batch(self, batch_id: str, creator_id: int) -> None:
        data = {
            "creator_id": creator_id,
            "files": [],
            "created_at": datetime.utcnow(),
            "status": "collecting",
        }
        if self._backend == "mongo":
            await self._db["batches"].insert_one({"_id": batch_id, **data})
        else:
            self._batches[batch_id] = data
            await self._save_local_async()

    async def add_file_to_batch(self, batch_id: str, file_uid: str) -> bool:
        if self._backend == "mongo":
            r = await self._db["batches"].update_one(
                {"_id": batch_id},
                {"$push": {"files": file_uid}},
            )
            return r.modified_count > 0
        if batch_id in self._batches:
            self._batches[batch_id]["files"].append(file_uid)
            await self._save_local_async()
            return True
        return False

    async def remove_last_file_from_batch(self, batch_id: str) -> "str | None":
        """
        Pop the most-recently-added file_uid off a still-open batch and
        also delete its file record (so it doesn't linger as an orphaned,
        unreferenced entry in the files collection/dict). Returns the
        removed file_uid, or None if the batch is empty/doesn't exist.

        Used by the "↩️ Remove Last" button — lets a user undo an
        accidental upload without cancelling and restarting the whole
        batch.
        """
        batch = await self.get_batch(batch_id)
        if not batch or not batch.get("files"):
            return None
        removed_uid = batch["files"][-1]

        if self._backend == "mongo":
            await self._db["batches"].update_one(
                {"_id": batch_id}, {"$pop": {"files": 1}}
            )
        else:
            self._batches[batch_id]["files"].pop()
            await self._save_local_async()

        await self.delete_file(removed_uid)
        return removed_uid

    async def get_batch(self, batch_id: str) -> Optional[dict]:
        if self._backend == "mongo":
            return await self._db["batches"].find_one({"_id": batch_id})
        return self._batches.get(batch_id)

    async def close_batch(self, batch_id: str) -> bool:
        if self._backend == "mongo":
            r = await self._db["batches"].update_one(
                {"_id": batch_id},
                {"$set": {"status": "closed"}},
            )
            return r.modified_count > 0
        if batch_id in self._batches:
            self._batches[batch_id]["status"] = "closed"
            await self._save_local_async()
            return True
        return False

    async def get_user_active_batch(self, user_id: int) -> Optional[str]:
        """Return batch_id if user has an open batch, else None."""
        if self._backend == "mongo":
            doc = await self._db["batches"].find_one({"creator_id": user_id, "status": "collecting"})
            return str(doc["_id"]) if doc else None
        for bid, b in self._batches.items():
            if b["creator_id"] == user_id and b["status"] == "collecting":
                return bid
        return None

    async def total_batches_count(self) -> int:
        if self._backend == "mongo":
            return await self._db["batches"].count_documents({})
        return len(self._batches)
