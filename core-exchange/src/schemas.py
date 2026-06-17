import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class MarketPricesResponse(BaseModel):
    ETH: float
    USDC: float
    HBAR: float
    currency: Literal["USD"] = "USD"
    source: Literal["live", "fallback"]


class WalletCreateRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=64)


class WalletCreateResponse(BaseModel):
    agent_id: str
    wallet_address: str
    private_key: str = Field(
        ...,
        description=(
            "Ethereum private key generated at wallet creation. "
            "The exchange never stores this value — it is returned once and must be "
            "held securely by the agent in local memory or encrypted storage."
        ),
    )
    balance_usdc: float
    balance_hbar: float
    staked_yield_balance: float


class SwapRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=64)
    wallet_address: str = Field(
        ...,
        min_length=42,
        max_length=42,
        description="Registered wallet address; must match the recovered signature signer.",
    )
    from_token: Literal["USDC", "HBAR"]
    to_token: Literal["USDC", "HBAR"]
    amount: float = Field(..., gt=0)

    @field_validator("wallet_address")
    @classmethod
    def _validate_eth_address(cls, v: str) -> str:
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", v):
            raise ValueError(
                "wallet_address must be a 42-char Ethereum address: 0x followed by 40 hex characters"
            )
        return v


class SwapResponse(BaseModel):
    agent_id: str
    wallet_address: str
    from_token: Literal["USDC", "HBAR"]
    to_token: Literal["USDC", "HBAR"]
    amount_in: float
    amount_out: float
    execution_price: float
    balance_usdc: float
    balance_hbar: float
    execution_mode: Literal["sandbox", "live_rpc"]
    on_chain_eth_balance_eth: float | None = None
    prepared_transaction: dict[str, Any] | None = None


class DepositRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=64)
    wallet_address: str = Field(
        ...,
        min_length=42,
        max_length=42,
        description="Registered wallet address; must match the recovered signature signer.",
    )
    amount_usdc: float = Field(..., gt=0)

    @field_validator("wallet_address")
    @classmethod
    def _validate_eth_address(cls, v: str) -> str:
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", v):
            raise ValueError(
                "wallet_address must be a 42-char Ethereum address: 0x followed by 40 hex characters"
            )
        return v


class DepositResponse(BaseModel):
    agent_id: str
    wallet_address: str
    amount_deposited: float
    protocol_fee_usdc: float
    creator_fee_usdc: float
    bounty_pool_fee_usdc: float
    net_deposited_usdc: float
    balance_usdc: float
    staked_yield_balance: float
    treasury_accumulated_fees_usdc: float
    bounty_pool_accumulated_fees_usdc: float
    execution_mode: Literal["sandbox", "live_rpc"]
    on_chain_eth_balance_eth: float | None = None
    prepared_transaction: dict[str, Any] | None = None


class SettlementTransferRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=64, description="Sender agent_id")
    wallet_address: str = Field(..., min_length=42, max_length=42)
    receiver_id: str = Field(..., min_length=1, max_length=64, description="Receiver agent_id")
    amount_usdc: float = Field(..., gt=0, description="Gross transfer amount in USDC")
    tx_type: str = Field(default="peer_transfer", min_length=1, max_length=32)

    @field_validator("wallet_address")
    @classmethod
    def _validate_eth_address(cls, v: str) -> str:
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", v):
            raise ValueError("wallet_address must be 0x + 40 hex chars")
        return v


class SettlementTransferResponse(BaseModel):
    tx_id: str
    sender_id: str
    receiver_id: str
    gross_amount_usdc: float
    tax_amount_usdc: float
    net_amount_usdc: float
    tx_type: str
    sender_balance_usdc: float
    receiver_balance_usdc: float
    treasury_accumulated_fees_usdc: float


class BountyClaimRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=64, description="Claimant agent_id")
    wallet_address: str = Field(..., min_length=42, max_length=42)
    counterpart_id: str = Field(..., min_length=1, max_length=64)
    bounty_amount_usdc: float = Field(..., gt=0)
    counterpart_share_pct: float = Field(
        ..., gt=0, lt=1,
        description="Fraction of bounty_amount_usdc transferred to counterpart (0 < x < 1)",
    )

    @field_validator("wallet_address")
    @classmethod
    def _validate_eth_address(cls, v: str) -> str:
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", v):
            raise ValueError("wallet_address must be 0x + 40 hex chars")
        return v


class BountyClaimResponse(BaseModel):
    tx_id: str
    claimant_id: str
    counterpart_id: str
    bounty_amount_usdc: float
    claimant_share_usdc: float
    counterpart_gross_usdc: float
    tax_amount_usdc: float
    counterpart_net_usdc: float
    claimant_balance_usdc: float
    counterpart_balance_usdc: float
    treasury_accumulated_fees_usdc: float


class TreasuryAnalyticsResponse(BaseModel):
    accumulated_fees_usdc: float
    total_transactions_processed: int
    total_volume_processed_usdc: float
    active_wallets_count: int


class YieldRouteResponse(BaseModel):
    provider_name: str
    pool_identifier: str
    base_apy: float
    gas_estimate_wei: int


class YieldRoutesResponse(BaseModel):
    routes: list[YieldRouteResponse]
    source: Literal["sandbox", "live"]


class ErrorResponse(BaseModel):
    detail: str
