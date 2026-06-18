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
    wallet_address: str | None = Field(
        default=None,
        description=(
            "Optional pre-derived Ethereum address (HD wallet flow). "
            "When supplied, the server registers this address as-is and does not "
            "generate a keypair — the caller holds the private key."
        ),
    )

    @field_validator("wallet_address")
    @classmethod
    def _validate_wallet_address(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"0x[0-9a-fA-F]{40}", v):
            raise ValueError("wallet_address must be 0x + 40 hex chars")
        return v


class WalletCreateResponse(BaseModel):
    agent_id: str
    wallet_address: str
    private_key: str = Field(
        default="",
        description=(
            "Ethereum private key generated at wallet creation. "
            "Empty string when the address was client-provided (HD wallet flow) — "
            "the server never held the key. "
            "The exchange never stores this value; it is returned once and must be "
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
    agent_id: str = Field(
        ..., min_length=1, max_length=64, description="Sender agent_id"
    )
    wallet_address: str = Field(..., min_length=42, max_length=42)
    receiver_id: str = Field(
        ..., min_length=1, max_length=64, description="Receiver agent_id"
    )
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
    agent_id: str = Field(
        ..., min_length=1, max_length=64, description="Claimant agent_id"
    )
    wallet_address: str = Field(..., min_length=42, max_length=42)
    counterpart_id: str = Field(..., min_length=1, max_length=64)
    bounty_amount_usdc: float = Field(..., gt=0)
    counterpart_share_pct: float = Field(
        ...,
        gt=0,
        lt=1,
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


class AnalyticsStatsResponse(BaseModel):
    total_transactions_processed: int
    total_volume_processed_usdc: float
    active_wallets_count: int
    success_rate_pct: float
    failure_count: int
    avg_latency_ms: float


class AnalyticsTreasuryResponse(BaseModel):
    accumulated_fees_usdc: float
    total_volume_processed_usdc: float


class RecentTransactionItem(BaseModel):
    tx_id: str
    sender_id: str
    receiver_id: str
    gross_amount_usdc: float
    tax_amount_usdc: float
    net_amount_usdc: float
    tx_type: str
    created_at: int
    on_chain_status: str | None = None


class ErrorResponse(BaseModel):
    detail: str


# ---------------------------------------------------------------------------
# Arbitrage router schemas
# ---------------------------------------------------------------------------


class RebalanceRequest(BaseModel):
    target_agent_id: str = Field(..., min_length=1, max_length=64)
    volume_usdc: float = Field(
        ...,
        gt=0,
        description="Gross volume to initiate at the first relay hop",
    )
    slippage_tolerance_pct: float = Field(
        default=0.005,
        ge=0.0,
        le=0.10,
        description="Safety floor = volume × this fraction; rebalance triggers when target is below it",
    )


class RebalanceHop(BaseModel):
    hop: int
    sender_id: str
    receiver_id: str
    gross_amount_usdc: float
    tax_amount_usdc: float
    net_amount_usdc: float
    tx_id: str


class RebalanceResponse(BaseModel):
    rebalanced: bool
    target_agent_id: str
    volume_usdc: float
    relay_path: list[str]  # 3 relay agent IDs (does not include target)
    transactions: list[RebalanceHop]
    pre_balance_usdc: float
    post_balance_usdc: float
    total_tax_usdc: float
    rejection_reason: str | None = None


class ArbitrageRouteRequest(BaseModel):
    entry_asset: Literal["USDC", "HBAR"]
    exit_asset: Literal["USDC", "HBAR"]
    volume_usdc: float = Field(..., gt=0, description="Total arbitrage volume in USDC")
    agent_chain: list[str] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Ordered sequence of agent_ids forming the routing path",
    )
    slippage_tolerance_pct: float = Field(
        default=0.005,
        ge=0.0,
        le=0.10,
        description="Maximum acceptable slippage as a fraction (0.005 = 0.5%)",
    )

    @field_validator("agent_chain")
    @classmethod
    def _no_blank_ids(cls, v: list[str]) -> list[str]:
        if any(not aid.strip() for aid in v):
            raise ValueError("agent_chain entries must be non-empty strings")
        return v


class ArbitrageStepResult(BaseModel):
    step: int
    agent_id: str
    balance_usdc: float
    slippage_floor_usdc: float
    balance_sufficient: bool
    pending_sync_blocked: bool
    wallet_found: bool


class ArbitrageRouteResponse(BaseModel):
    viable: bool
    entry_asset: str
    exit_asset: str
    volume_usdc: float
    agent_chain: list[str]
    slippage_tolerance_pct: float
    steps: list[ArbitrageStepResult]
    total_slippage_usdc: float
    expected_output_usdc: float
    expected_output_native: float = (
        0.0  # output in exit_asset units; equals expected_output_usdc when exit_asset == "USDC"
    )
    rejection_reason: str | None = None


# ---------------------------------------------------------------------------
# Swarm telemetry schemas
# ---------------------------------------------------------------------------


class SwarmDeskState(BaseModel):
    name: str
    balance_usdc: float = 0.0
    transfers_ok: int = 0
    transfers_err: int = 0
    eth_balance: float = 0.0


class SwarmAnalyticsResponse(BaseModel):
    swarm_active: bool
    log_lines: list[str]
    desks: list[SwarmDeskState]
    iterations: int | None = None
    route_checks: int | None = None
    viable_routes: int | None = None
    last_activity: str | None = None
    equalization_count: int = 0
    equalization_volume_usdc: float = 0.0


class SwarmHeartbeatRequest(BaseModel):
    iterations: int
    route_checks: int
    viable_routes: int
    dry_run: bool = False
    desks: list[SwarmDeskState]
    equalization_count: int = 0
    equalization_volume_usdc: float = 0.0


# ---------------------------------------------------------------------------
# Treasury breakdown by tx_type
# ---------------------------------------------------------------------------


class TxTypeBreakdown(BaseModel):
    tx_type: str
    count: int
    total_volume_usdc: float
    total_tax_usdc: float


class TreasuryBreakdownResponse(BaseModel):
    accumulated_fees_usdc: float
    equalization_fees_usdc: float
    platform_treasury_address: str | None
    tx_type_breakdown: list[TxTypeBreakdown]


# ---------------------------------------------------------------------------
# Protocol parameters
# ---------------------------------------------------------------------------


class ProtocolParamsResponse(BaseModel):
    tax_rate_pct: float
    tax_rate_fraction: float
    min_transfer_usdc: float
    safety_floor_pct: float
    relay_hops: int
    candidate_cap: int
    gas_cost_per_hop_usdc: float
    execution_mode: str
    platform_treasury_address: str | None
    protocol_domain: str


# ---------------------------------------------------------------------------
# Path scanner (expanded non-mutating simulation)
# ---------------------------------------------------------------------------


class PathScanResult(BaseModel):
    path: list[str]
    viable: bool
    steps: list[ArbitrageStepResult]
    expected_output_usdc: float
    total_slippage_usdc: float
    rejection_reason: str | None = None


class ScanPathsRequest(BaseModel):
    candidate_agents: list[str] = Field(
        ...,
        min_length=2,
        max_length=20,
        description="Pool of agent_ids to generate routes from",
    )
    volume_usdc: float = Field(..., gt=0)
    slippage_tolerance_pct: float = Field(default=0.005, ge=0.0, le=0.10)
    path_length: int = Field(
        default=3,
        ge=2,
        le=5,
        description="Number of hops per path (2–5)",
    )
    max_paths: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of distinct paths to evaluate",
    )

    @field_validator("candidate_agents")
    @classmethod
    def _no_blank_ids(cls, v: list[str]) -> list[str]:
        if any(not aid.strip() for aid in v):
            raise ValueError("candidate_agents entries must be non-empty strings")
        return v


class ScanPathsResponse(BaseModel):
    total_paths_checked: int
    viable_count: int
    volume_usdc: float
    slippage_tolerance_pct: float
    paths: list[PathScanResult]
