# VectraFi Roadmap

## Phase 1: Local Scaffolding & Mocking (Complete)

- [x] Python virtual environment and core dependencies
- [x] FastAPI core exchange with SQLite agent state
- [x] Mock REST endpoints: market prices, wallet create, trade swap, bank deposit
- [x] OpenClaw `exchange-bank-gateway` skill manifest with tool JSON schemas

## Phase 2: Web3 Connectivity & Hardened Auth (Complete)

- [x] Ethereum keypair generation via `eth_account` at wallet creation
- [x] One-time private key return (never persisted server-side)
- [x] Signed request verification on swap and deposit routes

## Phase 3: Web3 Infrastructure Connectivity (Complete)

- [x] Live market pricing via Coinbase public API (ETH/USD, USDC/USD, HBAR/USD)
- [x] Per-asset and full-network fallback pricing schema
- [x] Dual-mode routing: sandbox SQLite ledger vs live RPC Web3 provider
- [x] On-chain balance checks and prepared transaction payloads when RPC active

## Next Priority Task

Publish to GitHub and open the OpenClaw skill registry for community adoption (Phase 3 launch milestone).
