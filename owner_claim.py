"""
Rotating, console-only owner-claim code.

Gates the first-run "whoever messages the bot first becomes admin" flow
(see plugins/setup.py). As soon as the bot starts, a random 6-digit code is
generated and printed straight to the console/log — and replaced with a
fresh one every ROTATE_SECONDS. Whoever wants to claim the bot has to
read the *current* code off the live process output at that moment; it's
never written to a file, never sent over Telegram by the bot itself, and
expires on its own.
"""

import asyncio
import random
import time

ROTATE_SECONDS = 120
_CODE_DIGITS = 6

_current_code: str | None = None
_previous_code: str | None = None  # accepted for one extra rotation as a grace window
_generated_at: float = 0.0


def _generate() -> str:
    global _current_code, _previous_code, _generated_at
    _previous_code = _current_code
    _current_code = f"{random.randint(0, 10 ** _CODE_DIGITS - 1):0{_CODE_DIGITS}d}"
    _generated_at = time.time()
    return _current_code


def check(candidate: str) -> bool:
    """True if `candidate` matches the current code, or the immediately
    previous one (so a code that rotates mid-typing doesn't reject someone
    who was already correct)."""
    candidate = (candidate or "").strip()
    if not candidate:
        return False
    return candidate in (_current_code, _previous_code) and (
        _current_code is not None or _previous_code is not None
    )


def seconds_remaining() -> int:
    return max(0, ROTATE_SECONDS - int(time.time() - _generated_at))


async def run(on_rotate) -> None:
    """
    Background task: generate a new code immediately, hand it to
    `on_rotate(code, seconds_valid)` for printing, then repeat every
    ROTATE_SECONDS. Intended to be launched with asyncio.create_task() and
    cancelled once setup completes (the bot process restarts on setup
    completion anyway, which stops it naturally).
    """
    while True:
        code = _generate()
        on_rotate(code, ROTATE_SECONDS)
        await asyncio.sleep(ROTATE_SECONDS)
