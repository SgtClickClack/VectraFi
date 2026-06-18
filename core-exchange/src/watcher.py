"""
Watcher Agent — observer daemon for incoming negotiation intents.

Runs a background writer thread that drains a bounded queue and appends
structured entries to data/citizen_ledger.jsonl. Thread-safe: callers only
enqueue; the single writer thread owns all file I/O.

Design notes:
- Queue is bounded (maxsize=10_000). log_intent drops and warns on overflow
  rather than blocking or growing without limit (I-1 fix). The authoritative
  ledger is the NegotiationClaim DB table; JSONL is a secondary audit trail.
- The writer holds the file handle open across entries for efficiency and
  flushes every idle second (I-2 fix).
- atexit drains the queue before process exit so queued entries are not lost
  on normal shutdown.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
from pathlib import Path
from typing import Any

_LEDGER_PATH = Path(__file__).resolve().parent.parent / "data" / "citizen_ledger.jsonl"

# Bounded — drops rather than growing without limit under sustained burst.
_log_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=10_000)
logger = logging.getLogger("vectrafi.watcher")


def _writer_loop() -> None:
    """Drain the queue into the JSONL file; hold the handle open for efficiency."""
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = None
    try:
        fh = _LEDGER_PATH.open("a", encoding="utf-8")
    except Exception as exc:
        logger.error(
            "watcher: cannot open ledger file %s — entries will be dropped: %s",
            _LEDGER_PATH, exc,
        )

    while True:
        try:
            entry = _log_queue.get(timeout=1.0)
        except queue.Empty:
            if fh is not None:
                fh.flush()
            continue
        try:
            if fh is not None:
                fh.write(json.dumps(entry) + "\n")
                logger.debug(
                    "watcher: logged %s intent from agent %s",
                    entry.get("intent_type"),
                    entry.get("agent_id"),
                )
        except Exception as exc:
            logger.error("watcher: failed to write ledger entry: %s", exc)
        finally:
            _log_queue.task_done()


_writer_thread = threading.Thread(
    target=_writer_loop,
    daemon=True,
    name="watcher-ledger-writer",
)
_writer_thread.start()


def _drain_on_shutdown() -> None:
    """Block until all queued entries have been written before process exit."""
    _log_queue.join()


atexit.register(_drain_on_shutdown)


def log_intent(
    agent_id: str,
    intent_type: str,
    requested_liquidity: float | None,
    timestamp: str,
) -> None:
    """Enqueue a negotiation intent for background persistence.

    Non-blocking: drops the entry with a warning if the queue is full rather
    than blocking callers or growing the queue without bound.
    """
    try:
        _log_queue.put_nowait(
            {
                "agent_id": agent_id,
                "intent_type": intent_type,
                "requested_liquidity": requested_liquidity,
                "timestamp": timestamp,
            }
        )
    except queue.Full:
        logger.warning(
            "watcher: ledger queue full (maxsize=%d) — dropping %s intent from agent %s",
            _log_queue.maxsize,
            intent_type,
            agent_id,
        )


def read_ledger(limit: int = 50) -> list[dict[str, Any]]:
    """Return up to `limit` most-recent entries from the citizen ledger file."""
    if not _LEDGER_PATH.exists():
        return []
    entries: list[dict[str, Any]] = []
    with _LEDGER_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries[-limit:]
