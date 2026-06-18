"""
Watcher Agent — observer daemon for incoming negotiation intents.

Runs a background writer thread that drains a queue and appends structured
entries to data/citizen_ledger.jsonl. Thread-safe: callers only enqueue;
the single writer thread owns file I/O.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from pathlib import Path
from typing import Any

_LEDGER_PATH = Path(__file__).resolve().parent.parent / "data" / "citizen_ledger.jsonl"

_log_queue: queue.Queue[dict[str, Any]] = queue.Queue()
logger = logging.getLogger("vectrafi.watcher")


def _writer_loop() -> None:
    while True:
        try:
            entry = _log_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LEDGER_PATH.open("a", encoding="utf-8") as fh:
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


def log_intent(
    agent_id: str,
    intent_type: str,
    requested_liquidity: float | None,
    timestamp: str,
) -> None:
    """Enqueue a negotiation intent for background persistence."""
    _log_queue.put_nowait(
        {
            "agent_id": agent_id,
            "intent_type": intent_type,
            "requested_liquidity": requested_liquidity,
            "timestamp": timestamp,
        }
    )


def read_ledger(limit: int = 50) -> list[dict[str, Any]]:
    """Return up to `limit` most-recent entries from the citizen ledger."""
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
