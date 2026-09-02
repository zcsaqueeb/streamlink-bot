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
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import info

logger = logging.getLogger(__name__)
LOCAL_DB_PATH: str = os.environ.get("LOCAL_DB_PATH", "local_db.json").strip() or "local_db.json"


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
            for k in self._DT_FIELDS:
                v = doc.get(k)
                if isinstance(v, str):
                    try:
                        doc[k] = datetime.fromisoformat(v)
                    except Exception:
                        pass
            return doc

        self._files   = {k: _revive(v) for k, v in snap.get("files", {}).items()}
        self._batches = {k: _revive(v) for k, v in snap.get("batches", {}).items()}
        self._users   = {int(k): _revive(v) for k, v in snap.get("users", {}).items()}
        if isinstance(snap.get("stats"), dict):
            self._stats.update(snap["stats"])
        logger.info(
            "Loaded local DB snapshot: %d files, %d batches, %d users.",
            len(self._files), len(self._batches), len(self._users),
        )

    def _save_local(self):
        """Persist the in-memory store to disk. No-op for the Mongo backend."""
        if self._backend != "memory":
            return

        def _plain(doc: dict) -> dict:
            out = {}
            for k, v in doc.items():
                out[k] = v.isoformat() if isinstance(v, datetime) else v
            return out

        snap = {
            "files":   {k: _plain(v) for k, v in self._files.items()},
            "batches": {k: _plain(v) for k, v in self._batches.items()},
            "users":   {str(k): _plain(v) for k, v in self._users.items()},
            "stats":   dict(self._stats),
        }
        try:
            tmp = LOCAL_DB_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, LOCAL_DB_PATH)   # atomic write — never a half file
        except Exception as e:
            logger.warning("Could not persist local DB snapshot: %s", e)

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
            await asyncio.to_thread(self._save_local)

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
            await self._db["batches"].create_index([("creator_id", 1), ("status", 1)])
        except Exception as e:
            logger.warning("Could not create indexes (non-fatal): %s", e)

    # ── Active Sessions ───────────────────────────────────────────────────────

    async def mark_active(self, user_id: int):
        """Mark user as active (last seen now)."""
        self._active_sessions[user_id] = datetime.utcnow()
        if self._backend == "mongo":
            await self._db["users"].update_one(
                {"_id": user_id},
                {"$set": {"last_seen": datetime.utcnow()}},
            )

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
            existing = await col.find_one({"_id": user_id})
            if existing:
                # Keep stored name/username fresh in case the user changed them.
                await col.update_one(
                    {"_id": user_id},
                    {"$set": {"name": name, "username": username, "last_seen": datetime.utcnow()}},
                )
                return False
            await col.insert_one({
                "_id": user_id, "name": name, "username": username,
                "joined": datetime.utcnow(), "banned": False,
                "last_seen": datetime.utcnow(),
            })
            return True
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
            for uid, data in self._users.items():
                yield {"_id": uid, **data}

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
        await self._save_local_async()

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
