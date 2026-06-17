"""
VectraFi Sandbox Micro-Bank Ledger
====================================
Sandbox simulation — workspace/ only. Targets workspace/bank.db (SQLite).

All balances are integer units (no floating-point). The 1.5% micro-tax is
computed in integer arithmetic: tax = amount * 15 // 1000, with any remainder
kept by the sender (rounds down in favour of precision over extraction).

NOTE: 'identifier' column in wallets is a plain label, not cryptographic
key material. Private keys are never generated or stored (CLAUDE.md §core rules).
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent / "bank.db"
_TREASURY_ID = "treasury"
_MICRO_TAX_BPS = 15          # 15 basis points = 1.5%
_INITIAL_AGENT_BALANCE = 10_000
_INITIAL_TREASURY_BALANCE = 0


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS wallets (
    agent_id    TEXT PRIMARY KEY,
    identifier  TEXT NOT NULL,         -- plain label; NOT cryptographic key material
    balance     INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    tx_id       TEXT PRIMARY KEY,
    sender_id   TEXT NOT NULL,
    receiver_id TEXT NOT NULL,
    gross_amount  INTEGER NOT NULL,
    tax_amount    INTEGER NOT NULL,
    net_amount    INTEGER NOT NULL,
    tx_type     TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    FOREIGN KEY (sender_id)   REFERENCES wallets(agent_id),
    FOREIGN KEY (receiver_id) REFERENCES wallets(agent_id)
);

CREATE TABLE IF NOT EXISTS micro_taxes (
    tax_id      TEXT PRIMARY KEY,
    tx_id       TEXT NOT NULL,
    from_agent  TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    FOREIGN KEY (tx_id) REFERENCES transactions(tx_id)
);
"""

_SEED_WALLETS = [
    (_TREASURY_ID,  "treasury-label",    _INITIAL_TREASURY_BALANCE),
    ("agent-zero",  "agent-zero-label",  _INITIAL_AGENT_BALANCE),
    ("agent-one",   "agent-one-label",   _INITIAL_AGENT_BALANCE),
]


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def get_connection(db_path: Path = _DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _tx(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Initialisation & seeding
# ---------------------------------------------------------------------------

def init_db(db_path: Path = _DB_PATH) -> None:
    """Create schema and seed wallets if the DB is uninitialised."""
    conn = get_connection(db_path)
    with _tx(conn):
        conn.executescript(_DDL)
        for agent_id, identifier, balance in _SEED_WALLETS:
            conn.execute(
                "INSERT OR IGNORE INTO wallets (agent_id, identifier, balance, created_at) "
                "VALUES (?, ?, ?, ?)",
                (agent_id, identifier, balance, _now()),
            )
    conn.close()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _now() -> int:
    return int(time.time())


def get_balance(agent_id: str, conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT balance FROM wallets WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown agent: {agent_id!r}")
    return row[0]


def get_wallet(agent_id: str, db_path: Path = _DB_PATH) -> dict:
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT agent_id, identifier, balance, created_at FROM wallets WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    conn.close()
    if row is None:
        raise KeyError(f"Unknown agent: {agent_id!r}")
    return {"agent_id": row[0], "identifier": row[1], "balance": row[2], "created_at": row[3]}


def list_wallets(db_path: Path = _DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT agent_id, identifier, balance, created_at FROM wallets ORDER BY agent_id"
    ).fetchall()
    conn.close()
    return [{"agent_id": r[0], "identifier": r[1], "balance": r[2], "created_at": r[3]}
            for r in rows]


def list_transactions(db_path: Path = _DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT tx_id, sender_id, receiver_id, gross_amount, tax_amount, "
        "net_amount, tx_type, ts FROM transactions ORDER BY ts"
    ).fetchall()
    conn.close()
    keys = ["tx_id", "sender_id", "receiver_id", "gross_amount",
            "tax_amount", "net_amount", "tx_type", "ts"]
    return [dict(zip(keys, r)) for r in rows]


# ---------------------------------------------------------------------------
# Core transaction engine
# ---------------------------------------------------------------------------

def execute_agent_transaction(
    sender_id: str,
    receiver_id: str,
    amount: int,
    tx_type: str,
    db_path: Path = _DB_PATH,
) -> dict:
    """
    Execute a micro-tax-deducted transfer between two agent wallets.

    Steps (all within a single SQL transaction):
      1. Verify sender balance >= amount; raise InsufficientFunds otherwise.
      2. Compute tax = amount * 15 // 1000  (integer, rounds down).
      3. Debit full `amount` from sender.
      4. Credit `tax` to treasury.
      5. Credit `net_amount = amount - tax` to receiver.
      6. Log to `transactions` and `micro_taxes`.

    Returns the completed transaction record.
    """
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")
    if sender_id == receiver_id:
        raise ValueError("sender and receiver must differ")

    tax_amount = amount * _MICRO_TAX_BPS // 1000
    net_amount = amount - tax_amount
    tx_id = str(uuid.uuid4())
    now = _now()

    conn = get_connection(db_path)
    try:
        with _tx(conn):
            sender_balance = get_balance(sender_id, conn)
            if sender_balance < amount:
                raise InsufficientFunds(
                    f"{sender_id} has {sender_balance} units; needs {amount}"
                )

            # Debit sender
            conn.execute(
                "UPDATE wallets SET balance = balance - ? WHERE agent_id = ?",
                (amount, sender_id),
            )
            # Credit treasury (tax)
            conn.execute(
                "UPDATE wallets SET balance = balance + ? WHERE agent_id = ?",
                (tax_amount, _TREASURY_ID),
            )
            # Credit receiver (net)
            conn.execute(
                "UPDATE wallets SET balance = balance + ? WHERE agent_id = ?",
                (net_amount, receiver_id),
            )
            # Log transaction
            conn.execute(
                "INSERT INTO transactions "
                "(tx_id, sender_id, receiver_id, gross_amount, tax_amount, net_amount, tx_type, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tx_id, sender_id, receiver_id, amount, tax_amount, net_amount, tx_type, now),
            )
            # Log micro-tax event
            tax_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO micro_taxes (tax_id, tx_id, from_agent, amount, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (tax_id, tx_id, sender_id, tax_amount, now),
            )
    finally:
        conn.close()

    return {
        "tx_id": tx_id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "gross_amount": amount,
        "tax_amount": tax_amount,
        "net_amount": net_amount,
        "tx_type": tx_type,
        "ts": now,
    }


class InsufficientFunds(Exception):
    pass
