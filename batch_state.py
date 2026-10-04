"""
Shared in-memory batch mode state.
Imported by both file_handler and batch plugins — no circular dependency.
"""

# user_id → batch_id for users currently in batch mode
_batch_mode: dict = {}


def is_in_batch(user_id: int) -> bool:
    return user_id in _batch_mode


def get_batch_id(user_id: int):
    return _batch_mode.get(user_id)


def set_batch(user_id: int, batch_id: str):
    _batch_mode[user_id] = batch_id


def clear_batch(user_id: int):
    _batch_mode.pop(user_id, None)
