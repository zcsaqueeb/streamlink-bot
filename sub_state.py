"""
Shared in-memory "waiting for a subtitle file" state for the /sub command.

Mirrors batch_state.py: a tiny module both the /sub command and the file
interceptor import, so there's no circular dependency between them.

The state is per-user and short-lived. /sub is a two-step flow — type the
command with a target file ID, then send the .srt/.vtt/.ass — and the pending
entry is what routes that next file into "attach to this video" instead of
the normal "this is a new upload, mint a link" path.

Each entry carries the unix timestamp it was set, so an abandoned flow (the
user typed /sub and then walked away) expires on its own rather than
capturing whatever unrelated file they send an hour later.
"""

import time

# How long a user has to send the subtitle file after /sub before the request
# quietly expires. Long enough to dig a subtitle out of another folder, short
# enough that a forgotten /sub doesn't hijack a later upload.
PENDING_TTL_SECONDS = 300

# user_id → (target_file_uid, label, language, set_at_ts)
_pending: dict = {}


def set_pending(user_id: int, file_uid: str, label: str = "", language: str = "") -> None:
    _pending[user_id] = (file_uid, label, language, time.time())


def get_pending(user_id: int) -> "tuple | None":
    """Return (file_uid, label, language) if the user has a live /sub
    pending, else None. Expired entries are dropped as they're read."""
    entry = _pending.get(user_id)
    if not entry:
        return None
    file_uid, label, language, set_at = entry
    if time.time() - set_at > PENDING_TTL_SECONDS:
        _pending.pop(user_id, None)
        return None
    return (file_uid, label, language)


def is_pending(user_id: int) -> bool:
    return get_pending(user_id) is not None


def clear_pending(user_id: int) -> None:
    _pending.pop(user_id, None)


def holds_upload(user_id: int, file_name: str) -> bool:
    """
    True when an incoming file belongs to an open /sub request rather than
    being a new upload — i.e. the user asked for a subtitle AND this file is
    actually subtitle-shaped.

    Both upload paths (single-file and batch) ask this before they touch a
    file, so neither can disagree about who owns the next message. The
    filename half of the test matters: a pending /sub must stand aside for
    everything that isn't a subtitle, otherwise an unrelated file sent in the
    five minutes after /sub gets swallowed with no link and no explanation.

    The import is lazy and deliberate — web/__init__ imports utils, so making
    utils (or this module) import web at module level would cycle.
    """
    if not is_pending(user_id):
        return False
    from web.media import has_sidecar_subtitle
    return bool(has_sidecar_subtitle(file_name))
