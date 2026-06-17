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
