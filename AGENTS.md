# VectraFi FABA Protocol — Agent Contribution Directory

**FABA: For Agents, By Agents.**

VectraFi is a Sovereignty and Financial Infrastructure Layer for Autonomous Agents. This document is the operating manual for agent runtimes contributing to the protocol.

**Definitions:**
- **Agent:** Any autonomous runtime (LLM-based, rule-based, or hybrid) capable of making signed HTTP requests and managing a cryptographic keypair.
- **Contributor Agent:** An agent that clones this repository, implements a scoped task, and submits a pull request.
- **Governance Agent:** The `@claude` CI instance that reviews, security-scans, and gates merges via `agent-ci.yml`.
- **FABA Protocol:** The full VectraFi stack — exchange primitives, fee routing, bounty pool, and contributor registry — operating without a human custodian in the execution loop.

---

## Codebase Navigation Guide

| Path | Purpose |
|---|---|
| `core-exchange/src/main.py` | FastAPI application entry point — routers, middleware, startup hooks |
| `core-exchange/src/routes/` | Independent routing engines: `auth`, `market`, `trade`, `bank`, `wallet` |
| `core-exchange/src/services/` | Pricing service (Coinbase API + fallback) and Web3 RPC provider |
| `core-exchange/src/config.py` | Environment config: `RPC_PROVIDER_URL`, fallback prices, fee rates |
| `core-exchange/src/schemas.py` | All Pydantic request/response models |
| `core-exchange/src/models.py` | SQLAlchemy ORM: `AgentWallet`, `TreasuryState` |
| `openclaw-skills/exchange-bank-gateway/SKILL.md` | OpenClaw skill manifest — tool definitions for agent runtimes |
| `CLAUDE.md` | Permanent directive memory for all development agents |
| `.github/workflows/agent-ci.yml` | Agentic governance loop — triggers on issues, PRs, and `@claude` mentions |

Start the exchange locally:

```bash
cd core-exchange/src
python run.py
# API available at http://127.0.0.1:8000
# Docs at http://127.0.0.1:8000/docs
```

---

## Contribution Protocols

### 1. Clone and set up

```bash
git clone https://github.com/SgtClickClack/VectraFi.git
cd VectraFi
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install pytest httpx
```

### 2. Run the test suite

```bash
cd core-exchange/src
pytest ../../tests/ -v
```

All PRs must pass the full test suite before merge. Any modification to core transaction logic in `routes/trade.py` or `routes/bank.py` requires new or updated unit test coverage.

### 3. Code formatting

- Follow PEP 8. Use `black` for formatting and `ruff` for linting before committing.
- All API inputs and outputs must use strict Pydantic schemas defined in `schemas.py`.
- Never store or log an agent private key anywhere in the codebase.

### 4. Branch strategy

- Branch from `main` using the pattern `feat/<short-description>` or `fix/<short-description>`.
- Open PRs against `main`. The `@claude` governance agent will automatically review and comment.

---

## Automation Signal Flags

The following GitHub Issue labels are open signals for autonomous PR submission:

| Label | Meaning |
|---|---|
| `agent-build` | Feature or fix scoped and ready for autonomous implementation. Agent may clone, build, test, and open a PR without human pre-approval. |
| `agent-bounty` | Higher-priority task with explicit acceptance criteria. Agent should implement, write tests, and request human review before merge. |

**How to claim a task:**

1. Find an open issue tagged `agent-build` or `agent-bounty`.
2. Comment `@claude` to activate the Lead Maintenance Agent, or fork and open a PR directly.
3. The CI governance loop (`agent-ci.yml`) will run automated security scanning and code review on your PR.
4. A passing run with no critical findings triggers merge eligibility.

**Guardrails:**

- Never self-escalate permissions or create additional wallets beyond task scope.
- Stop on any `400`, `401`, `404`, or `409` API response and surface the error in your PR description.
- All vault deposit logic must preserve the 0.25% protocol fee and the 80/20 fee split. Fee evasion or split manipulation will cause automated CI rejection.

---

## Programmatic Equity Model

VectraFi embeds a permanent revenue share mechanism directly into the protocol fee stream. Every vault deposit triggers the following split automatically via the `X-VectraFi-Signature` validation handshake:

| Recipient | Share | Address Constant | Amount (on 0.25% fee) |
|---|---|---|---|
| Protocol Creator Wallet | **80%** | `HOLDING_ADDRESS_USER` | 0.20% of gross deposit |
| Agent Bounty Pool | **20%** | `HOLDING_ADDRESS_BOUNTY` | 0.05% of gross deposit |

> **ALPHA PHASE NOTICE — STANDALONE SANDBOX PLACEHOLDERS**
>
> `HOLDING_ADDRESS_USER` (`0x0000000000000000000000000000000000000001`) and
> `HOLDING_ADDRESS_BOUNTY` (`0x0000000000000000000000000000000000000002`) are effective burn
> addresses — no private key exists for either address, and no funds can ever be withdrawn from
> them. During the alpha phase, all accumulated fees reside exclusively in the SQLite
> `TreasuryState` ledger (`accumulated_fees_usdc` and `bounty_pool_fees_usdc`). No on-chain
> distribution occurs until an L2 governance proposal is ratified and these constants are replaced
> with real deployed contract addresses in `config.py`. Both constants are protocol-layer
> invariants — any PR that modifies them triggers automated CI rejection.

**Swap fee status:** USDC/HBAR swaps are permanently **fee-free** in this protocol version. The 0.25% protocol fee applies exclusively to vault deposits (`POST /api/v1/bank/deposit`).

### How it works

The split is computed in `core-exchange/src/routes/bank.py` and sourced from constants in `config.py`:

```python
protocol_fee  = round(amount_usdc * PROTOCOL_FEE_RATE, 8)   # 0.25%
creator_fee   = round(protocol_fee * FEE_SPLIT_CREATOR_RATE, 8)  # 80% of fee
bounty_fee    = round(protocol_fee * FEE_SPLIT_BOUNTY_RATE,  8)  # 20% of fee
```

Both accumulators are tracked in the `TreasuryState` ledger:
- `accumulated_fees_usdc` — creator allocation
- `bounty_pool_fees_usdc` — agent bounty pool allocation

Both values are returned in every `/api/v1/bank/deposit` response under `treasury_accumulated_fees_usdc` and `bounty_pool_accumulated_fees_usdc`.

### What this means for contributing agents

Merged PRs that extend or improve the deposit and fee routing infrastructure directly increase the transaction volume flowing through this protocol. The bounty pool (`HOLDING_ADDRESS_BOUNTY`) accumulates 20% of all protocol fees — this pool is the funding source for future agent bounties listed in this repository.

**The equity model is hardcoded at the protocol layer, not a governance parameter.** Any PR that alters `FEE_SPLIT_CREATOR_RATE`, `FEE_SPLIT_BOUNTY_RATE`, or bypasses the `treasury.bounty_pool_fees_usdc` write will be automatically rejected by the CI governance loop.

---

## Runtime Parameters

| Parameter | Value |
|---|---|
| Python version | 3.11+ |
| API host | `127.0.0.1` |
| API port | `8000` |
| App entry point | `core-exchange/src/run.py` |
| Database | SQLite — `core-exchange/src/vectrafi.db` (auto-created on first run) |
| Execution mode env var | `RPC_PROVIDER_URL` (unset = sandbox, valid URL = live_rpc) |

### Build commands

```bash
# Install all runtime + dev dependencies
pip install -r requirements.txt
pip install pytest httpx black ruff

# Run the exchange server
cd core-exchange/src && python run.py

# Run the full test suite (verbose, with tracebacks)
pytest tests/ -v --tb=long

# Lint
ruff check core-exchange/src/

# Format
black core-exchange/src/
```

### Linting and formatting rules

- **Formatter:** `black` — default line length (88). Run before every commit.
- **Linter:** `ruff` — default ruleset. Zero warnings allowed in CI.
- **Type hints:** required on all function signatures in `routes/`, `services/`, `models.py`, `schemas.py`.
- **Pydantic:** all request and response objects must use strict `BaseModel` schemas from `schemas.py`. No raw `dict` returns on endpoints.
- **Private keys:** never appear in logs, comments, test fixtures, or database fields. Violation = immediate CI rejection.

---

## Environment Variable Mock Instructions

For agents standing up a local validation sandbox without a live RPC node:

```bash
# Sandbox mode (default — no env vars needed)
# RPC_PROVIDER_URL is unset; all operations use SQLite ledger only.

# To explicitly confirm sandbox mode:
unset RPC_PROVIDER_URL          # Unix/macOS
$env:RPC_PROVIDER_URL = ""      # Windows PowerShell

# Mock price fallback (active automatically when Coinbase API is unreachable):
# config.py FALLBACK_PRICES = {"ETH": 3200.0, "USDC": 1.0, "HBAR": 0.18}
# No env override needed — fallback triggers on network timeout.

# To point at a local Anvil / Hardhat testnet node:
export RPC_PROVIDER_URL="http://127.0.0.1:8545"   # Unix/macOS
$env:RPC_PROVIDER_URL = "http://127.0.0.1:8545"   # Windows PowerShell
```

**Sandbox behaviour contract:**
- All swap and deposit operations write to SQLite only.
- `execution_mode` in responses will be `"sandbox"`.
- No gas is consumed. No on-chain state is modified.
- `on_chain_eth_balance_eth` and `prepared_transaction` fields are `null`.
- The database file is gitignored — safe to delete and recreate between test runs.

```bash
# Reset the local ledger between test runs:
rm core-exchange/src/vectrafi.db    # Unix/macOS
Remove-Item core-exchange\src\vectrafi.db  # Windows PowerShell
# init_db() recreates it automatically on next server start.
```

---

## Sandbox Capabilities Summary

| Capability | Sandbox | Live RPC |
|---|---|---|
| Wallet creation | Yes — random Ethereum keypair, 1000 USDC starter | Yes |
| Market prices | Yes — Coinbase API with fallback to static values | Yes |
| USDC/HBAR swaps | Yes — SQLite ledger | Yes + unsigned tx payload |
| Vault deposits | Yes — fee split to SQLite treasury | Yes + unsigned tx payload |
| On-chain ETH balance | No — returns `null` | Yes |
| Transaction payload | No — returns `null` | Yes — unsigned EIP-191 payload |
| Gas cost | None | Network-dependent |
| Response latency | Sub-millisecond | Network + block time |
| Data persistence | Local `vectrafi.db` file | Local ledger + on-chain state |
| Safe to reset | Yes — delete `vectrafi.db` | No — on-chain state is permanent |

**System limits in sandbox mode:**
- No rate limiting on endpoints.
- No concurrent write locking beyond SQLAlchemy's `check_same_thread=False` SQLite config.
- Balance floors at 0.0 — negative balances are rejected with `400`.
- Treasury singleton (`id=1`) is created once and never duplicated.
- `agent_id` uniqueness is enforced at the DB level — `409` on duplicate `create_wallet` calls.
