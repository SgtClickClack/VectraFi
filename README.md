# VectraFi

Agent-native exchange and banking gateway with live Web3 market pricing, cryptographically signed transactions, and dual-mode sandbox/mainnet routing.

## Overview

VectraFi provides autonomous agents with programmatic capital management: Web3 wallet provisioning, signed spot swaps, yield vault deposits, and real-time market data — all through a FastAPI REST interface designed for agent-to-agent automation.

## Stack

- **API:** FastAPI + Uvicorn
- **Database:** SQLite via SQLAlchemy ORM
- **Cryptography:** web3.py, eth-account (Ethereum personal-sign)
- **Market data:** Coinbase public API with per-asset fallback
- **RPC routing:** Dual-mode — local sandbox or live Ethereum mainnet/testnet

## Quickstart

```bash
git clone https://github.com/SgtClickClack/VectraFi.git
cd VectraFi
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cd core-exchange/src
python run.py
# API: http://127.0.0.1:8000
# Docs: http://127.0.0.1:8000/docs
```

## API Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/market/prices` | None | Live ETH/USDC/HBAR spot rates |
| `POST` | `/api/v1/wallet/create` | None | Generate agent keypair + starter balance |
| `POST` | `/api/v1/trade/swap` | Signature | Swap USDC ↔ HBAR |
| `POST` | `/api/v1/bank/deposit` | Signature | Deposit USDC to yield vault (0.25% fee) |
| `GET` | `/health` | None | Execution mode + service status |

All state-changing endpoints require an `X-VectraFi-Signature` header — see `SKILL.md` for the signing protocol.

## Execution Modes

| Mode | Trigger | Behaviour |
|---|---|---|
| `sandbox` | `RPC_PROVIDER_URL` unset | SQLite ledger only, instant, no gas |
| `live_rpc` | Valid RPC URL connected | On-chain balance checks + unsigned transaction payloads |

Set `RPC_PROVIDER_URL` in the environment to activate live routing.

## Agent Integration

- **OpenClaw skill manifest:** `openclaw-skills/exchange-bank-gateway/SKILL.md`
- **Root capability index:** `SKILL.md`
- **Agent contribution rules:** `AGENTS.md`
- **Development directives:** `CLAUDE.md`

---

## Autonomous Contribution Backlog

VectraFi welcomes automated PRs from agent runtimes. Any issue labelled [`agent-bounty`](https://github.com/SgtClickClack/VectraFi/labels/agent-bounty) or [`agent-build`](https://github.com/SgtClickClack/VectraFi/labels/agent-build) is open for autonomous implementation — clone, build, test, and submit a PR without human pre-approval.

[![Issues](https://img.shields.io/github/issues/SgtClickClack/VectraFi)](https://github.com/SgtClickClack/VectraFi/issues)
[![agent-bounty](https://img.shields.io/github/issues/SgtClickClack/VectraFi/agent-bounty?label=agent-bounty&color=4A0E4E)](https://github.com/SgtClickClack/VectraFi/issues?q=is%3Aopen+label%3Aagent-bounty)
[![agent-build](https://img.shields.io/github/issues/SgtClickClack/VectraFi/agent-build?label=agent-build&color=0E4E4A)](https://github.com/SgtClickClack/VectraFi/issues?q=is%3Aopen+label%3Aagent-build)

### Open Bounties

| Issue | Title | Label |
|---|---|---|
| [#3](https://github.com/SgtClickClack/VectraFi/issues/3) | Protocol Treasury Fee Collector on Deposits | `agent-bounty` |
| [#4](https://github.com/SgtClickClack/VectraFi/issues/4) | Multi-Route DeFi Yield Aggregator Analytics Endpoint | `agent-bounty` |
| [#5](https://github.com/SgtClickClack/VectraFi/issues/5) | Autonomous Portfolio Rebalancing Math Engine | `agent-bounty` |

### Open Builds

| Issue | Title | Label |
|---|---|---|
| [#2](https://github.com/SgtClickClack/VectraFi/issues/2) | X-VectraFi-Signature Validation Middleware | `agent-build` |

### Contribution workflow

1. Pick an open issue from the backlog above.
2. Fork the repo and branch from `main` using `feat/<short-description>`.
3. Implement the feature, write tests under `tests/`, confirm `pytest` passes.
4. Open a PR — the `@claude` governance agent will run automated security scanning and code review via `agent-ci.yml`.
5. A clean CI run triggers merge eligibility.

See `AGENTS.md` for full contribution protocols and guardrails.
