# VectraFi — Machine-Readable Capability Manifest

This file provides a structured capability index for external agent runtimes. For the full OpenClaw skill definition including JSON schemas, see `openclaw-skills/exchange-bank-gateway/SKILL.md`.

## Runtime Requirements

- API base: `http://127.0.0.1:8000` (override via `VECTRAFI_API_BASE_URL`)
- Optional live routing: set `RPC_PROVIDER_URL` to an Ethereum JSON-RPC endpoint
- All state-changing calls require header: `X-VectraFi-Signature`

---

## Capability: `verify_signature`

**Purpose:** Validate that an incoming request is cryptographically authorized by the registered agent wallet.

**Trigger:** Automatically applied to `/api/v1/trade/swap` and `/api/v1/bank/deposit` by the auth middleware.

**Implementation:** `core-exchange/src/routes/auth.py` → `verify_signed_payload()`

**Protocol:**
1. Extract raw JSON body bytes from the request.
2. Recover signer address via Ethereum personal-sign (`encode_defunct` + `Account.recover_message`).
3. Compare recovered address against `wallet_address` in the payload (case-insensitive).
4. Verify `wallet_address` matches the registered address for `agent_id` in the SQLite ledger.
5. Return `400` on malformed body, `401` on signature mismatch, `404` on unknown agent.

**Signing example (Python):**
```python
from eth_account import Account
from eth_account.messages import encode_defunct

body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
msg = encode_defunct(body_bytes)
signed = Account.sign_message(msg, private_key=private_key)
headers = {"X-VectraFi-Signature": signed.signature.hex()}
```

---

## Capability: `process_deposit`

**Purpose:** Move liquid USDC from an agent's wallet into the yield vault, applying the protocol fee.

**Endpoint:** `POST /api/v1/bank/deposit`

**Authentication:** `X-VectraFi-Signature` required.

**Implementation:** `core-exchange/src/routes/bank.py` → `deposit_to_vault()`

**Request schema:**
```json
{
  "agent_id": "string (1–64 chars)",
  "wallet_address": "string (42-char checksummed Ethereum address)",
  "amount_usdc": "number (> 0)"
}
```

**Response fields:**
```json
{
  "amount_deposited": "number",
  "protocol_fee_usdc": "number",
  "net_deposited_usdc": "number",
  "balance_usdc": "number",
  "staked_yield_balance": "number",
  "treasury_accumulated_fees_usdc": "number",
  "execution_mode": "sandbox | live_rpc"
}
```

**Execution modes:**
- `sandbox` — SQLite ledger update only; instant, no gas cost.
- `live_rpc` — On-chain ETH balance check + unsigned transaction payload returned for agent signing.

---

## Capability: `route_treasury_fee`

**Purpose:** Automatically calculate and route the 0.25% protocol fee on every vault deposit to the VectraFi treasury.

**Implementation:** `core-exchange/src/routes/bank.py` (lines 60–71) + `core-exchange/src/models.py` → `TreasuryState`

**Fee logic:**
```
protocol_fee  = round(amount_usdc * 0.0025, 8)
net_deposited = round(amount_usdc - protocol_fee, 8)

wallet.balance_usdc        -= amount_usdc
wallet.staked_yield_balance += net_deposited
treasury.accumulated_fees_usdc += protocol_fee
```

**Treasury address (on-chain reference):** `0x0000000000000000000000000000000000000001`

**Guardrails for contributing agents:**
- Fee rate is defined in `config.py` as `PROTOCOL_FEE_RATE = 0.0025`. Do not hardcode `0.0025` elsewhere.
- All fee arithmetic must use `round(..., 8)` to preserve floating-point consistency across the ledger.
- Fee evasion (setting fee to zero or bypassing the treasury write) will cause automated CI rejection via `agent-ci.yml`.

---

## Index

| Capability | Endpoint | Auth | Implementation |
|---|---|---|---|
| `verify_signature` | middleware | — | `routes/auth.py` |
| `process_deposit` | `POST /api/v1/bank/deposit` | signature | `routes/bank.py` |
| `route_treasury_fee` | internal | — | `routes/bank.py`, `models.py` |
| `execute_swap` | `POST /api/v1/trade/swap` | signature | `routes/trade.py` |
| `get_market_prices` | `GET /api/v1/market/prices` | none | `routes/market.py` |
| `create_wallet` | `POST /api/v1/wallet/create` | none | `routes/wallet.py` |
