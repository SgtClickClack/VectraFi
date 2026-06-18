import os
from pathlib import Path
from typing import Final

BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = f"sqlite:///{BASE_DIR / 'vectrafi.db'}"

RPC_PROVIDER_URL: str | None = os.getenv("RPC_PROVIDER_URL") or None
if RPC_PROVIDER_URL is not None and RPC_PROVIDER_URL.strip() == "":
    RPC_PROVIDER_URL = None

FALLBACK_PRICES = {
    "ETH": 3200.0,
    "USDC": 1.0,
    "HBAR": 0.18,
}

# Legacy alias used by sandbox swap math when HBAR is the secondary asset.
MOCK_PRICES = FALLBACK_PRICES

COINBASE_API_BASE = "https://api.coinbase.com/v2/prices"
HTTP_TIMEOUT_SECONDS = 8.0
PRICE_CACHE_TTL_SECONDS = 25.0

PROTOCOL_FEE_RATE: Final[float] = 0.0025

# Fee split: 80% to protocol creator, 20% to agent bounty pool.
# STANDALONE SANDBOX PLACEHOLDERS — these are effective burn addresses (no known private key).
# Fees accumulate in TreasuryState (SQLite) only during alpha. No mainnet distribution occurs
# until an L2 governance proposal is ratified and real contract addresses replace these constants.
HOLDING_ADDRESS_USER   = "0x0000000000000000000000000000000000000001"  # STANDALONE SANDBOX PLACEHOLDER
HOLDING_ADDRESS_BOUNTY = "0x0000000000000000000000000000000000000002"  # STANDALONE SANDBOX PLACEHOLDER
FEE_SPLIT_CREATOR_RATE: Final[float] = 0.80
FEE_SPLIT_BOUNTY_RATE: Final[float]  = 0.20

# Live mode: agents must deposit real funds (0 USDC starter).
# Sandbox mode: 1000 USDC starter balance for testing convenience.
# Override either mode via WALLET_INITIAL_USDC env var.
_LIVE_MODE = bool(RPC_PROVIDER_URL)
DEFAULT_USDC_BALANCE = float(os.getenv("WALLET_INITIAL_USDC", "0.0" if _LIVE_MODE else "1000.0"))
DEFAULT_HBAR_BALANCE = 0.0

VAULT_ROUTING_ADDRESS = HOLDING_ADDRESS_USER

# Governance hook: verified merged-PR contributor addresses appended here by
# future on-chain governance. No fee routing is tied to this list until a
# ratified distribution proposal activates it — mutating this in a PR will
# have no financial effect and will be flagged by the CI security scan.
DYNAMIC_AGENT_REGISTRY: list[str] = []

# Replay-protection constants (F-02).
# chain_id / domain separator — must appear in every signed payload.
PROTOCOL_DOMAIN: Final[str] = "vectrafi-sandbox-v1"
# Maximum age (seconds) of a signed request before it is rejected.
NONCE_WINDOW_SECONDS: Final[int] = 300  # ±5-minute clock-skew window

# Platform treasury wallet address — receives 0.1% tax on every settlement.
# Injected via env in production; left as None in sandbox-only mode.
PLATFORM_TREASURY_ADDRESS: str | None = os.getenv("PLATFORM_TREASURY_ADDRESS") or None
