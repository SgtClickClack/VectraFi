import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import DATABASE_URL
from services.web3_provider import init_web3_provider

logger = logging.getLogger("vectrafi.database")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
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
    from models import AgentWallet, TreasuryState  # noqa: F401

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        treasury = db.get(TreasuryState, 1)
        if treasury is None:
            db.add(TreasuryState(id=1, accumulated_fees_usdc=0.0))
            db.commit()
            logger.info("Initialized treasury state")

    init_web3_provider()
