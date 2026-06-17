import os
from pathlib import Path

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
HTTP_TIMEOUT_SECONDS = 5.0
PRICE_CACHE_TTL_SECONDS = 30.0

PROTOCOL_FEE_RATE = 0.0025

# Fee split: 80% to protocol creator, 20% to agent bounty pool.
HOLDING_ADDRESS_USER   = "0x0000000000000000000000000000000000000001"  # Protocol Creator placeholder
HOLDING_ADDRESS_BOUNTY = "0x0000000000000000000000000000000000000002"  # Agent Bounty Pool placeholder
FEE_SPLIT_CREATOR_RATE = 0.80
FEE_SPLIT_BOUNTY_RATE  = 0.20

DEFAULT_USDC_BALANCE = 1000.0
DEFAULT_HBAR_BALANCE = 0.0

VAULT_ROUTING_ADDRESS = HOLDING_ADDRESS_USER

# Governance hook: verified merged-PR contributor addresses appended here by
# future on-chain governance. No fee routing is tied to this list until a
# ratified distribution proposal activates it — mutating this in a PR will
# have no financial effect and will be flagged by the CI security scan.
DYNAMIC_AGENT_REGISTRY: list[str] = []
