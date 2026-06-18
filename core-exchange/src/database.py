import logging
import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from services.web3_provider import init_web3_provider

logger = logging.getLogger("vectrafi.database")

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./vectrafi.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_is_sqlite = DATABASE_URL.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}

_pool_kwargs: dict = {}
if not _is_sqlite:
    _pool_kwargs = {
        "pool_pre_ping": True,  # detect stale Railway proxy connections before use
        "pool_size": 5,
        "max_overflow": 10,
        "pool_recycle": 300,  # recycle connections every 5 min to avoid proxy drops
    }

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    **_pool_kwargs,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from models import (  # noqa: F401
        AgentWallet,
        SettlementTransaction,
        TreasuryState,
        UsedNonce,
    )

    Base.metadata.create_all(bind=engine)

    # Inline migration: add bounty_pool_fees_usdc to existing SQLite databases.
    # Skipped on PostgreSQL — the column is created by create_all() above.
    if _is_sqlite:
        with engine.connect() as conn:
            existing = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(treasury_state)"))
            }
            if "bounty_pool_fees_usdc" not in existing:
                conn.execute(
                    text(
                        "ALTER TABLE treasury_state ADD COLUMN bounty_pool_fees_usdc REAL NOT NULL DEFAULT 0.0"
                    )
                )
                conn.commit()
                logger.info(
                    "Migrated treasury_state: added bounty_pool_fees_usdc column"
                )

    with SessionLocal() as db:
        treasury = db.get(TreasuryState, 1)
        if treasury is None:
            from decimal import Decimal

            db.add(
                TreasuryState(
                    id=1,
                    accumulated_fees_usdc=Decimal("0"),
                    bounty_pool_fees_usdc=Decimal("0"),
                )
            )
            db.commit()
            logger.info("Initialized treasury state")

    init_web3_provider()
