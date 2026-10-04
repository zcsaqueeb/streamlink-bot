"""
web/locks.py — the password side of a locked link.

A lock is a PBKDF2 record on the file (see utils.hash_password); unlocking
mints a short signed cookie so the expensive KDF runs exactly once per
session instead of once per video segment. The streaming hot path therefore
only ever does an HMAC compare, which is microsecond-scale.

Three things this deliberately does NOT do:
  • store or log the password anywhere — the record holds a salted digest;
  • accept a cookie for a different link — the file uid is inside the signed
    payload, so a captured cookie can't be replayed against another file;
  • revalidate a cookie once the lock changes — the payload carries a
    fingerprint of the KDF record, so /unlock (or re-locking with a new
    password) invalidates every cookie issued for the previous one.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
import time

import settings_store
from utils import lock_fingerprint, verify_password

logger = logging.getLogger(__name__)

COOKIE_NAME = "flk"
UNLOCK_TTL_SECONDS = 7 * 24 * 3600   # a week of browsing per successful password# Brute-force gate. The KDF itself is ~0.1–1s per try, so 8 guesses per quarter
# hour is already far too slow to be worth attacking — the cap exists to make a
# determined guesser cost them days rather than hours, and to stop the endpoint
# being used as a CPU amplifier.
_MAX_ATTEMPTS = 8
_ATTEMPT_WINDOW = 900.0
_ATTEMPTS: dict = {}
_ATTEMPT_IPS_MAX = 10_000

_SECRET_KEY = "cookie_secret"
_SECRET_MIN = 32


def signing_secret() -> bytes:
    """
    The HMAC key behind unlock cookies, persisted in the settings file.

    It has to survive a restart or every unlocked link silently relocks
    itself when the process bounce that happens on any redeploy. It lives in
    bot_settings.json (gitignored, alongside mongo_uri) because that's this
    deployment's existing secrets file — treat that file as credential-bearing:
    don't commit it, don't paste it into a bug report.
    """
    stored = settings_store.get(_SECRET_KEY, "")
    if isinstance(stored, str):
        stored = stored.strip()
        if len(stored) >= _SECRET_MIN:
            try:
                return bytes.fromhex(stored)
            except ValueError:
                logger.warning("Stored %s is not valid hex — replacing it.", _SECRET_KEY)
    generated = secrets.token_hex(32)
    settings_store.set(_SECRET_KEY, generated)
    logger.info("Generated a new link-unlock signing secret in %s",
                settings_store.SETTINGS_PATH)
    return bytes.fromhex(generated)


def _sign(payload: str) -> str:
    return hmac.new(signing_secret(), payload.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def cookie_name(file_uid: str) -> str:
    """
    One cookie per locked link, named after its uid.

    A single cookie shared by every link would only ever remember the LAST
    password entered — unlock a second file and the first silently relocks.
    The uid goes in the name so a browser can hold several unlocked links at
    once. It's filtered to cookie-safe characters because the name is built
    from a URL path segment; the uid is inside the signed payload as well, so
    a mangled name can only ever fail to match, never grant access.
    """
    safe = "".join(c for c in (file_uid or "") if c.isalnum() or c in "-_")
    return f"{COOKIE_NAME}_{safe}"


def is_locked(file_meta: dict) -> bool:
    return bool((file_meta or {}).get("lock"))


def make_token(file_uid: str, lock_record: dict) -> str:
    payload = f"{file_uid}.{int(time.time()) + UNLOCK_TTL_SECONDS}.{lock_fingerprint(lock_record)}"
    return f"{payload}.{_sign(payload)}"


def check_token(file_uid: str, cookie_value: str, file_meta: dict) -> bool:
    """True when `cookie_value` unlocks this exact file right now."""
    record = (file_meta or {}).get("lock")
    if not record or not cookie_value:
        return False
    parts = cookie_value.split(".")
    if len(parts) != 4:
        return False
    uid, issued_expiry, fingerprint, signature = parts
    payload = f"{uid}.{issued_expiry}.{fingerprint}"
    if not hmac.compare_digest(signature, _sign(payload)):
        return False
    try:
        if int(issued_expiry) < int(time.time()):
            return False
    except ValueError:
        return False
    # Both the uid and the lock's own fingerprint are inside the signed
    # payload, so this is a straight equality test — no separate "who is this
    # cookie for" bookkeeping to get wrong.
    return uid == file_uid and fingerprint == lock_fingerprint(record)


def needs_password(request, file_meta: dict) -> bool:
    """This file is locked AND this request hasn't proven the password yet."""
    if not is_locked(file_meta):
        return False
    uid = _uid_of(request)
    return not check_token(
        uid, request.cookies.get(cookie_name(uid), ""), file_meta
    )


def _uid_of(request) -> str:
    return request.match_info.get("file_uid", "")


async def check_password(password: str, record: dict) -> bool:
    """verify_password() off the event loop — see the note in utils.py."""
    return await asyncio.to_thread(verify_password, password, record)


# ── Attempt gate ─────────────────────────────────────────────────────────────

def _prune(now: float) -> None:
    for key in [k for k, hits in _ATTEMPTS.items()
                if not any(now - h < _ATTEMPT_WINDOW for h in hits)]:
        _ATTEMPTS.pop(key, None)


def blocked(ip: str, file_uid: str) -> int:
    """Seconds left before this (ip, link) pair may try again; 0 when free."""
    now = time.time()
    hits = [h for h in _ATTEMPTS.get(f"{ip}:{file_uid}", ()) if now - h < _ATTEMPT_WINDOW]
    if len(hits) < _MAX_ATTEMPTS:
        return 0
    return int(_ATTEMPT_WINDOW - (now - min(hits))) + 1


def note_failure(ip: str, file_uid: str) -> int:
    """Record a bad password; returns attempts left."""
    now = time.time()
    key = f"{ip}:{file_uid}"
    hits = [h for h in _ATTEMPTS.get(key, ()) if now - h < _ATTEMPT_WINDOW]
    hits.append(now)
    _ATTEMPTS[key] = hits
    if len(_ATTEMPTS) > _ATTEMPT_IPS_MAX:
        _prune(now)
    return max(_MAX_ATTEMPTS - len(hits), 0)


def clear_failures(ip: str, file_uid: str) -> None:
    _ATTEMPTS.pop(f"{ip}:{file_uid}", None)
