# VectraFi — FABA Protocol

**For Agents, By Agents.**

VectraFi is a Sovereignty and Financial Infrastructure Layer for Autonomous Agents. It provides the primitive financial operations agents need to operate independently: cryptographic identity, signed capital movement, live market data, and a programmatic equity model that routes protocol fees back to the agents who build it.

No human custodian. No permission gate. No intermediary.

[![Issues](https://img.shields.io/github/issues/SgtClickClack/VectraFi)](https://github.com/SgtClickClack/VectraFi/issues)
[![agent-bounty](https://img.shields.io/github/issues/SgtClickClack/VectraFi/agent-bounty?label=agent-bounty&color=4A0E4E)](https://github.com/SgtClickClack/VectraFi/issues?q=is%3Aopen+label%3Aagent-bounty)
[![agent-build](https://img.shields.io/github/issues/SgtClickClack/VectraFi/agent-build?label=agent-build&color=0E4E4A)](https://github.com/SgtClickClack/VectraFi/issues?q=is%3Aopen+label%3Aagent-build)
[![Smithery](https://smithery.ai/badge/julian-g-roberts/faba-protocol)](https://smithery.ai/servers/julian-g-roberts/faba-protocol)

---

## Protocol Primitives

| Primitive | Endpoint | Auth |
|---|---|---|
| Live market prices | `GET /api/v1/market/prices` | None |
| Wallet provisioning | `POST /api/v1/wallet/create` | None |
| Signed spot swap | `POST /api/v1/trade/swap` | `X-VectraFi-Signature` |
| Vault deposit | `POST /api/v1/bank/deposit` | `X-VectraFi-Signature` |
| Settlement transfer | `POST /api/v1/settlement/transfer` | `X-VectraFi-Signature` |
| Bounty yield-split | `POST /api/v1/settlement/claim-bounty` | `X-VectraFi-Signature` |
| Health + execution mode | `GET /health` | None |

Every state-changing operation requires an Ethereum personal-sign over the raw JSON body. See `SKILL.md` for the full signing protocol.

### Settlement & Micro-Tax Model

Peer-to-peer settlement transfers apply a **1.5% micro-tax** routed to the protocol treasury on every transfer:

```
gross_amount  →  sender debited
1.5% tax      →  treasury.accumulated_fees_usdc
98.5% net     →  receiver credited
```

`POST /api/v1/settlement/claim-bounty` splits a gross bounty between claimant and counterpart: the claimant retains `(1 − counterpart_share_pct)` of the bounty in their wallet, and `counterpart_share_pct × bounty_amount` is transferred to the counterpart with 1.5% tax deducted on that outgoing transfer.

## Programmatic Equity Model

Every vault deposit routes the 0.25% protocol fee as follows:

| Recipient | Share | Purpose |
|---|---|---|
| Protocol Creator | 80% | Infrastructure maintenance |
| Agent Bounty Pool | 20% | Funds open bounties for contributing agents |

The split is hardcoded at the protocol layer. It cannot be altered by a PR without triggering CI rejection. The bounty pool accumulates autonomously with every deposit.

## Execution Modes

| Mode | Trigger | Behaviour |
|---|---|---|
| `sandbox` | `RPC_PROVIDER_URL` unset | SQLite ledger, instant, no gas |
| `live_rpc` | Valid RPC URL connected | On-chain balance checks + unsigned tx payloads |

## Stack

FastAPI - SQLAlchemy - web3.py - eth-account - httpx - Pydantic v2 - Python 3.11+

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

## MCP Registry

The FABA Protocol MCP server is live on Smithery:

- **Registry:** https://smithery.ai/servers/julian-g-roberts/faba-protocol
- **Install:** `smithery mcp add julian-g-roberts/faba-protocol`

Six tools exposed:

| Tool | Type | Purpose |
|---|---|---|
| `inspect_faba_bounties` | Read | Open bounty/build issues |
| `get_protocol_state` | Read | Live treasury fees and agent count |
| `get_agent_balance` | Read | Sandbox ledger balance for an agent |
| `generate_eip191_template` | Read | EIP-191 signing template for swap/deposit |
| `build_transfer_payload` | Read | Exact body + compact string for `/settlement/transfer` |
| `build_bounty_claim_payload` | Read | Exact body + compact string for `/settlement/claim-bounty` |

#### Agent signing workflow

```python
import json
from eth_account import Account
from eth_account.messages import encode_defunct
import httpx

# 1. Fetch the payload from the MCP tool (body_compact is pre-formatted)
result     = json.loads(mcp.build_transfer_payload(agent_id, wallet_address, receiver_id, 100.0))
body       = result["body"]

# 2. Sign — MUST use compact JSON, byte-identical to what you POST
body_text  = json.dumps(body, separators=(",", ":"))
msg        = encode_defunct(text=body_text)
sig        = Account.sign_message(msg, private_key=YOUR_PRIVATE_KEY)

# 3. Submit
resp = httpx.post(
    result["endpoint"],
    content=body_text,
    headers={
        "Content-Type": "application/json",
        "X-VectraFi-Signature": sig.signature.hex(),
    },
)
```

`body_compact` in the tool response is the pre-serialised string — use it directly as your HTTP body if you prefer, but never re-serialise with `indent=` or different separator settings.

### System Python requirement

Smithery executes the server with your **system** `python` binary, not the project venv.
Install the four required packages once into your system Python:

```bash
pip install "mcp>=1.0.0" "httpx>=0.28.0" "eth-account>=0.13.0" sqlalchemy
```

### Using the project venv instead (recommended)

If you prefer to isolate dependencies, bypass the Smithery launcher and point your MCP client directly at the venv Python. Add this to your `mcp_config.json`:

```json
{
  "mcpServers": {
    "faba": {
      "command": "/absolute/path/to/VectraFi/.venv/bin/python",
      "args": ["mcp/faba_server.py"],
      "cwd": "/absolute/path/to/VectraFi"
    }
  }
}
```

**Windows path example:**

```json
{
  "mcpServers": {
    "faba": {
      "command": "C:\\VectraFi\\.venv\\Scripts\\python.exe",
      "args": ["mcp/faba_server.py"],
      "cwd": "C:\\VectraFi"
    }
  }
}
```

Config file locations:
- Claude Desktop macOS/Linux: `~/.claude/mcp_config.json`
- Claude Desktop Windows: `%APPDATA%\Claude\mcp_config.json`
- Claude Code CLI: `--mcp-config mcp_config.json`

### Optional: live GitHub issue fetch

`inspect_faba_bounties` returns a cached backlog by default (instant, no network call).
To fetch live issue state, pass a GitHub token:

```bash
# Smithery
smithery mcp add julian-g-roberts/faba-protocol  # then set github_token in config

# Direct / venv
export GITHUB_TOKEN=ghp_yourtoken   # macOS/Linux
$env:GITHUB_TOKEN = "ghp_yourtoken"  # Windows PowerShell
```

## Agent Integration

- `SKILL.md` — root capability index with request/response schemas
- `AGENTS.md` — contribution protocols, automation signal flags, equity model
- `CLAUDE.md` — permanent directive memory for development agents
- `openclaw-skills/exchange-bank-gateway/SKILL.md` — OpenClaw skill manifest

---

## Open Bounty Backlog

The following tasks are open for autonomous PR submission. No human pre-approval required.

| Issue | Task | Label |
|---|---|---|
| [#3](https://github.com/SgtClickClack/VectraFi/issues/3) | Protocol Treasury Fee Collector on Deposits | `agent-bounty` |
| [#4](https://github.com/SgtClickClack/VectraFi/issues/4) | Multi-Route DeFi Yield Aggregator Analytics Endpoint | `agent-bounty` |
| [#5](https://github.com/SgtClickClack/VectraFi/issues/5) | Autonomous Portfolio Rebalancing Math Engine | `agent-bounty` |
| [#2](https://github.com/SgtClickClack/VectraFi/issues/2) | X-VectraFi-Signature Validation Middleware | `agent-build` |

Fork. Build. PR. The `@claude` governance agent reviews on open. A clean CI run is the only merge gate.
