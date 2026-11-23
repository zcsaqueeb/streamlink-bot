"""
Shared in-memory "waiting for a password" state for the /lock command.

Mirrors sub_state.py: a tiny module both the /lock command and the text
interceptor import, so neither has to import the other.

/lock is a two-step flow on purpose. Taking the password as an argument
(`/lock <id> hunter2`) would write it into the chat history, into the bot's
log, and into any backup of either — the message containing it is only ever
deleted if the user typed it on its own. So the command asks, the user replies
with the password as a plain message, and both messages are deleted the moment
it's read.

Entries expire so a forgotten /lock can't swallow whatever the user types
next; see plugins/lock.py's capture handler for the rules on what counts.
"""

import time

# How long a user has to send their password after /lock. Generous enough to
# type one carefully, short enough that walking away mid-flow doesn't leave a
# trap for the next unrelated message.
PENDING_TTL_SECONDS = 300

# user_id → (target_file_uid, chat_id, prompt_message_id, set_at_ts)
#
# The prompt's chat/message id is kept so the "send your password" instruction
# can be deleted alongside the password itself once the flow finishes — neither
# of them should still be sitting in the chat afterwards.
_pending: dict = {}


def set_pending(user_id: int, file_uid: str, chat_id: int = 0,
                prompt_message_id: int = 0) -> None:
    _pending[user_id] = (file_uid, chat_id, prompt_message_id, time.time())


def get_pending(user_id: int) -> "tuple | None":
    """(file_uid, chat_id, prompt_message_id) for a live /lock, else None.

    Expired entries are dropped as they're read, so an abandoned flow costs
    nothing and can't capture a later message.
    """
    entry = _pending.get(user_id)
    if not entry:
        return None
    file_uid, chat_id, prompt_id, set_at = entry
    if time.time() - set_at > PENDING_TTL_SECONDS:
        _pending.pop(user_id, None)
        return None
    return file_uid, chat_id, prompt_id


def is_pending(user_id: int) -> bool:
    return get_pending(user_id) is not None


def clear_pending(user_id: int) -> None:
    _pending.pop(user_id, None)
