import uuid

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class AgentWallet(Base):
    __tablename__ = "agent_wallets"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(66), unique=True, nullable=False)
    balance_usdc: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    balance_hbar: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    staked_yield_balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # TODO (faba-agent-bounty): issue #5 — add xrp_balance column when XRP is added as a
    # supported rebalancing asset in the portfolio engine.


class TreasuryState(Base):
    __tablename__ = "treasury_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    accumulated_fees_usdc: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    bounty_pool_fees_usdc: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class SettlementTransaction(Base):
    __tablename__ = "settlement_transactions"

    tx_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sender_id: Mapped[str] = mapped_column(String(64), nullable=False)
    receiver_id: Mapped[str] = mapped_column(String(64), nullable=False)
    gross_amount_usdc: Mapped[float] = mapped_column(Float, nullable=False)
    tax_amount_usdc: Mapped[float] = mapped_column(Float, nullable=False)
    net_amount_usdc: Mapped[float] = mapped_column(Float, nullable=False)
    tx_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


# TODO (faba-agent-bounty): issue #4 — add YieldRoute model with fields:
# provider_name(str) | pool_identifier(str) | base_apy(float) | gas_estimate_wei(int) | updated_at(datetime)
# Register in init_db() and expose via GET /api/v1/yield/routes.

# TODO (faba-agent-bounty): issue #5 — add RebalanceRecord model with fields:
# id(PK) | agent_id(FK->AgentWallet) | target_allocations(JSON) | executed_swaps(JSON) |
# pre_balances(JSON) | post_balances(JSON) | created_at(datetime)
# Persists each rebalance execution for audit and replay.
