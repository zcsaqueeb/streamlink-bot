"""
In-process counters for currently-running downloads/streams, used to give
/status a live view of throughput instead of only historical totals.
Intentionally simple (no locks): asyncio is single-threaded, and these are
plain int increments/decrements, so there's no real race to guard against.
"""

import time

active_transfers: int = 0
session_bytes_served: int = 0
_session_started = time.monotonic()


def transfer_started():
    global active_transfers
    active_transfers += 1


def transfer_finished(bytes_written: int = 0):
    global active_transfers, session_bytes_served
    active_transfers = max(0, active_transfers - 1)
    session_bytes_served += max(0, bytes_written)


def session_throughput_bps() -> float:
    elapsed = max(1.0, time.monotonic() - _session_started)
    return session_bytes_served / elapsed
