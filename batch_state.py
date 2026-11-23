"""
Shared in-memory batch mode state.
Imported by both file_handler and batch plugins — no circular dependency.
"""

import asyncio
import weakref

# user_id → batch_id for users currently in batch mode
_batch_mode: dict = {}

# One lock per user, so a single user's batch operations run one at a time and
# in arrival order. Updates are now handled in parallel, and a user sending an
# album of files used to have them copied and appended in whatever order the
# copies happened to finish — the links came out shuffled, and "/done" could
# run before the last files were in. Other users are never affected: the lock
# is per user. Weak values, so an idle user's lock is freed automatically.
_user_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()


def user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


def is_in_batch(user_id: int) -> bool:
    return user_id in _batch_mode


def get_batch_id(user_id: int):
    return _batch_mode.get(user_id)


def set_batch(user_id: int, batch_id: str):
    _batch_mode[user_id] = batch_id


def clear_batch(user_id: int):
    _batch_mode.pop(user_id, None)
