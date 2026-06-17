---
name: exchange-bank-gateway
description: Manage agent capital on VectraFi — create Web3 wallets, sign transactions, swap USDC/HBAR, and deposit to the yield vault via the core exchange API with live market data.
metadata: {"openclaw":{"requires":{"env":["VECTRAFI_API_BASE_URL"]},"envVars":[{"name":"VECTRAFI_API_BASE_URL","required":false,"description":"Base URL for the VectraFi core exchange API (default http://127.0.0.1:8000)"},{"name":"RPC_PROVIDER_URL","required":false,"description":"Optional Ethereum JSON-RPC URL enabling live on-chain routing mode"}],"tools":{"create_agent_wallet":{"description":"Register a new agent wallet with a cryptographically generated keypair and starter USDC balance.","endpoint":{"method":"POST","path":"/api/v1/wallet/create","auth":false},"parameters":{"type":"object","required":["agent_id"],"properties":{"agent_id":{"type":"string","minLength":1,"maxLength":64,"description":"Unique identifier for the autonomous agent."}},"additionalProperties":false},"response":{"type":"object","required":["agent_id","wallet_address","private_key","balance_usdc","balance_hbar","staked_yield_balance"],"properties":{"agent_id":{"type":"string"},"wallet_address":{"type":"string","description":"Checksummed Ethereum public address persisted by the exchange."},"private_key":{"type":"string","description":"Hex-encoded private key returned once. Never stored by the exchange — agent must retain securely."},"balance_usdc":{"type":"number"},"balance_hbar":{"type":"number"},"staked_yield_balance":{"type":"number"}}}},"execute_swap":{"description":"Execute a signed spot swap between USDC and HBAR using live or fallback market prices.","endpoint":{"method":"POST","path":"/api/v1/trade/swap","auth":{"header":"X-VectraFi-Signature","signs":"raw JSON request body bytes"}},"parameters":{"type":"object","required":["agent_id","wallet_address","from_token","to_token","amount"],"properties":{"agent_id":{"type":"string","minLength":1,"maxLength":64,"description":"Agent whose wallet will execute the swap."},"wallet_address":{"type":"string","minLength":42,"maxLength":42,"description":"Registered wallet address; must match recovered signature."},"from_token":{"type":"string","enum":["USDC","HBAR"],"description":"Token to sell."},"to_token":{"type":"string","enum":["USDC","HBAR"],"description":"Token to buy."},"amount":{"type":"number","exclusiveMinimum":0,"description":"Amount of from_token to swap."}},"additionalProperties":false},"response":{"type":"object","required":["agent_id","wallet_address","from_token","to_token","amount_in","amount_out","execution_price","balance_usdc","balance_hbar","execution_mode"],"properties":{"agent_id":{"type":"string"},"wallet_address":{"type":"string"},"from_token":{"type":"string","enum":["USDC","HBAR"]},"to_token":{"type":"string","enum":["USDC","HBAR"]},"amount_in":{"type":"number"},"amount_out":{"type":"number"},"execution_price":{"type":"number"},"balance_usdc":{"type":"number"},"balance_hbar":{"type":"number"},"execution_mode":{"type":"string","enum":["sandbox","live_rpc"]},"on_chain_eth_balance_eth":{"type":["number","null"]},"prepared_transaction":{"type":["object","null"]}}}},"deposit_to_vault":{"description":"Move USDC from liquid balance into the yield vault with a signed request; 0.25% protocol fee routes to treasury.","endpoint":{"method":"POST","path":"/api/v1/bank/deposit","auth":{"header":"X-VectraFi-Signature","signs":"raw JSON request body bytes"}},"parameters":{"type":"object","required":["agent_id","wallet_address","amount_usdc"],"properties":{"agent_id":{"type":"string","minLength":1,"maxLength":64,"description":"Agent whose USDC will be deposited."},"wallet_address":{"type":"string","minLength":42,"maxLength":42,"description":"Registered wallet address; must match recovered signature."},"amount_usdc":{"type":"number","exclusiveMinimum":0,"description":"Gross USDC amount to deposit before protocol fee."}},"additionalProperties":false},"response":{"type":"object","required":["agent_id","wallet_address","amount_deposited","protocol_fee_usdc","net_deposited_usdc","balance_usdc","staked_yield_balance","treasury_accumulated_fees_usdc","execution_mode"],"properties":{"agent_id":{"type":"string"},"wallet_address":{"type":"string"},"amount_deposited":{"type":"number"},"protocol_fee_usdc":{"type":"number"},"net_deposited_usdc":{"type":"number"},"balance_usdc":{"type":"number"},"staked_yield_balance":{"type":"number"},"treasury_accumulated_fees_usdc":{"type":"number"},"execution_mode":{"type":"string","enum":["sandbox","live_rpc"]},"on_chain_eth_balance_eth":{"type":["number","null"]},"prepared_transaction":{"type":["object","null"]}}}}}}}
---

# VectraFi Exchange & Bank Gateway

Use this skill when an agent needs programmatic access to capital management on VectraFi: Web3 wallet provisioning, cryptographically signed transactions, live-market spot swaps, and yield vault deposits.

## API base URL

Default: `http://127.0.0.1:8000`

Override with `VECTRAFI_API_BASE_URL` when the core exchange runs elsewhere.

Optional live routing: set `RPC_PROVIDER_URL` to an Ethereum JSON-RPC endpoint on the exchange host to enable on-chain balance checks and prepared transaction payloads.

Start the backend from the repo:

```bash
cd core-exchange/src
python run.py
```

## Registered tools

| Tool | HTTP | Auth | Purpose |
| --- | --- | --- | --- |
| `create_agent_wallet` | `POST /api/v1/wallet/create` | None | Generate keypair + starter USDC |
| `execute_swap` | `POST /api/v1/trade/swap` | `X-VectraFi-Signature` | Swap USDC ↔ HBAR |
| `deposit_to_vault` | `POST /api/v1/bank/deposit` | `X-VectraFi-Signature` | Stake USDC into yield vault |

## Live market data

`GET /api/v1/market/prices` now returns **authentic ecosystem spot rates** fetched from the public Coinbase API:

- **ETH/USD**
- **USDC/USD**
- **HBAR/USD** (with per-asset fallback if unavailable)

Example live response:

```json
{
  "ETH": 3245.12,
  "USDC": 1.0,
  "HBAR": 0.178,
  "currency": "USD",
  "source": "live"
}
```

If the public network times out or errors, the API returns the same schema with `"source": "fallback"` and static baseline values. Always read `source` before sizing trades.

## Execution modes (sandbox vs live RPC)

VectraFi operates in a dual-mode design:

| Mode | Trigger | Behavior |
| --- | --- | --- |
| **sandbox** | `RPC_PROVIDER_URL` unset or unreachable | SQLite ledger calculations only; instant local execution |
| **live_rpc** | Valid `RPC_PROVIDER_URL` connected | On-chain ETH balance checks + unsigned transaction payload preparation; SQLite ledger still tracks agent balances |

Swap and deposit responses include:

- `execution_mode`: `"sandbox"` or `"live_rpc"`
- `on_chain_eth_balance_eth`: present in live mode
- `prepared_transaction`: unsigned payload skeleton in live mode (agent must sign and broadcast separately)

**Timing and cost expectations:**

- **Sandbox** — Sub-millisecond ledger updates; no gas fees.
- **Live RPC** — Network latency for balance reads; on-chain broadcast adds block confirmation time and gas costs not reflected in sandbox balances.

Plan human budget caps accordingly and never assume sandbox timing in live mode.

## Private key custody (mandatory)

When you call `create_agent_wallet`, the response includes a **`private_key`** alongside `wallet_address`.

- **Store immediately** — Persist the `private_key` in your localized context, encrypted memory, or secure agent vault. The exchange **never** saves it; it cannot be retrieved later.
- **Never log or expose** — Do not paste the private key into chat, tickets, or public channels.
- **One wallet per agent_id** — Re-calling create returns `409` if the wallet already exists.

You need the stored private key to sign every swap and deposit API request.

## Request signing (mandatory for swaps and deposits)

Protected endpoints require header `X-VectraFi-Signature`.

1. Build the exact JSON body string you will send (UTF-8). The signature must cover **the identical bytes** in the HTTP body — no reformatting after signing.
2. Sign the body text with your stored `private_key` using Ethereum personal-sign (`encode_defunct` + `Account.sign_message`).
3. Send the hex signature (with or without `0x` prefix) in `X-VectraFi-Signature`.
4. Include `wallet_address` in the JSON body. The backend recovers the signer via `Account.recover_message()` and rejects mismatches with **401 Unauthorized**.

## Capital management workflow

1. **Provision once** — Call `create_agent_wallet`, then securely store `private_key` and `wallet_address`.
2. **Check live prices** — Read `/api/v1/market/prices` and confirm `source` before sizing swaps.
3. **Sign and swap** — Use `execute_swap` with a signed payload; inspect `execution_mode`.
4. **Sign and deposit** — Use `deposit_to_vault` with a signed payload. A **0.25% protocol fee** routes to treasury.

Always inspect returned balances after each operation before planning the next action.

## Human budget constraints (mandatory)

Autonomous agents must treat human-defined limits as hard stops, not suggestions.

- **Never exceed** a supervisor's daily spend cap, per-trade maximum, or total vault allocation without explicit human approval in the current session.
- **Confirm intent** before any deposit or swap that would leave liquid USDC below a stated emergency reserve (default: keep at least 10% of assigned capital liquid unless the human directs otherwise).
- **Stop on errors** — If the API returns `400`, `401`, `404`, or `409`, do not retry in a loop. Report the error payload and wait for human guidance.
- **Fee awareness** — Every vault deposit costs 0.25% to treasury. In live RPC mode, add expected gas costs on top.
- **No self-escalation** — Do not create additional wallets, increase swap sizes, or chain deposits to circumvent limits set by the human operator.

When uncertain about budget headroom, ask the human before moving capital.
