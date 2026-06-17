from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class AgentWallet(Base):
    __tablename__ = "agent_wallets"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(66), unique=True, nullable=False)
    balance_usdc: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    balance_hbar: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    staked_yield_balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class TreasuryState(Base):
    __tablename__ = "treasury_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    accumulated_fees_usdc: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    bounty_pool_fees_usdc: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
