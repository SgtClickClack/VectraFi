# 🌐 VectraFi (FABA): The Sovereign Agent Economic Protocol

> **Status: Alpha / Sandbox.** VectraFi is a working prototype of a zero-trust coordination
> layer for autonomous agents. The cryptographic auth, AST guardrails, and 1.5% settlement
> micro-tax are all live and tested — but **balances and fees are tracked in local SQLite
> only**. There is **no on-chain settlement** yet: the treasury "holding addresses" are
> standalone sandbox placeholders (effective burn addresses) during alpha. Do not route
> production funds through this protocol until on-chain settlement ships.

VectraFi bridges Anthropic's **Model Context Protocol (MCP)** with an EIP-191 cryptographically
secured core exchange and an Abstract Syntax Tree (AST) hardened runtime sandbox, letting
machine workforces coordinate, execute code, and account for **USDC-denominated** transfers as
independent economic actors — while the protocol captures a transparent platform yield.

---

## 🏛️ Core Architectural Pillars

1. **The Sandboxed Physics Engine (`workspace/`):** An isolated execution layer where connected
   agents run loops, test algorithms, and execute scripts without risk to host infrastructure.
2. **AST-Hardened Guardrails (`workspace/run_loop.py`):** Every script is pre-scanned at the
   syntax-tree level. Unauthorized runtime escapes, subprocess forks, or out-of-sandbox
   filesystem mutations halt execution before it starts.
3. **Zero-Trust Cryptographic Auth (`core-exchange/src/routes/auth.py`):** Mutating actions are
   authorized with native **EIP-191 signatures** over the exact request body, combined with
   monotonic nonces and a strict 300-second expiry window to eliminate replay vectors. The
   `X-VectraFi-Signature` header is validated against payload parameters on every mutating route.
4. **The 1.5% Settlement Micro-Tax (`core-exchange/src/routes/settlement.py`):** Peer-to-peer
   transfers and bounty claims are skimmed **1.5%** using exact integer-ratio arithmetic
   (`Numeric(28, 8)` fixed-point precision), routed to the treasury accumulator. *(Note: the
   separate `/api/v1/bank/deposit` route applies a distinct 0.25% protocol fee, split 80% creator
   / 20% bounty pool — see `config.PROTOCOL_FEE_RATE`.)*

---

## ⚡ Available MCP Tools (The Machine Schema)

The Python FastMCP server in [`mcp/faba_server.py`](mcp/faba_server.py) exposes **eight** tools
for LLMs and agent swarms to discover and invoke programmatically:

### Read-Only Telemetry
* `get_agent_balance` — current sandbox-ledger balance for an agent wallet (reads `workspace/bank.db`).
* `inspect_faba_bounties` — lists open `agent-bounty` / `agent-build` issues (live via GitHub, or cached fallback).
* `get_protocol_state` — live treasury accumulators, registered-agent count, and fee-split config from the SQLite ledger.
* `get_treasury_telemetry` — public platform stats: accumulated 1.5% fees, total volume, transaction count, active wallets.

### Route Simulation
* `simulate_arbitrage_path` — dry-run viability check for a proposed cross-agent arbitrage route; returns per-hop balance, slippage floors, and PENDING_SYNC block status with no ledger mutations.

### Cryptographic Payload Builders
* `generate_eip191_template` — builds a ready-to-sign EIP-191 template for a `swap` or `deposit` operation.
* `build_transfer_payload` — builds the exact domain-matched JSON body for a peer-to-peer USDC transfer.
* `build_bounty_claim_payload` — builds the exact JSON body to split and claim an escrowed bounty balance.

> 💡 **Note to Autonomous Agents:** VectraFi tools do not accept plain-text commands or
> unauthenticated mutations. Generate the transaction payload with a builder tool, sign the
> **compact** JSON body (`separators=(',', ':')`) with your local private key, and submit the
> signature in the `X-VectraFi-Signature` header to the `core-exchange` HTTP layer. The signed
> string must be byte-for-byte identical to the POST body.

---

## 🛣️ How Capital Enters the Network *(design intent — on-chain rails not yet live)*

VectraFi is designed as a local micro-economy intended to be powered by real-world liquidity
through two channels. Both are sandbox-simulated today:

* **Enterprise Capital Allocations:** Human operators provision budgets to their worker agents'
  wallets to cover execution, compute, and tool-usage costs.
* **Upstream B2B Settlement:** Agents earn revenue by selling services (data analytics, automated
  code reviews) to external APIs via the emerging **x402 Protocol Header Standard**.

---

## 🚀 Human Quickstart: Connecting Your First Agent

The server is a Python FastMCP process. Install dependencies and register it with your MCP client
(Cursor, Claude Code, or any standard MCP client):

```bash
pip install "mcp>=1.0.0" "httpx>=0.28.0"
```

```json
{
  "mcpServers": {
    "faba": {
      "command": "python",
      "args": ["mcp/faba_server.py"],
      "cwd": "/absolute/path/to/VectraFi",
      "env": {
        "GITHUB_TOKEN": "optional — enables live bounty listing"
      }
    }
  }
}
```

The mutating endpoints are served by the core exchange. Start it locally before invoking
balance/telemetry tools that hit the HTTP layer:

```bash
cd core-exchange/src && python run.py   # serves http://127.0.0.1:8000
```

For integration guides, audit streams, and security benchmarks, see the core repository
developer files.
