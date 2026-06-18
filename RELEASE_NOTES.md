# VectraFi Production Release — v0.4.0

**Release date:** 2026-06-19  
**Branch:** `main`  
**Test baseline:** 140 tests, 0 failures  
**Business plan reference:** [VectraFiBusinessPlan.html](VectraFiBusinessPlan.html) — all 6 milestones addressed

---

## 5 Roadmap Gaps Closed

All open gaps from `Project-Roadmap.md` are now resolved and covered by automated tests.

### Gap 1 — NegotiationClaim Persistence ✅

**Milestone §3 | Prerequisite: None**

`NegotiationClaim` ORM model added to [`models.py`](core-exchange/src/models.py) with full lifecycle fields:
- `negotiation_id`, `agent_id`, `intent_type`, `status` (queued → evaluating → granted | rejected)
- `requested_liquidity_usdc`, `proposed_toll_share_pct`, `target_corridor`, `metadata_json`
- `evaluation_reason`, `created_at`, `updated_at`, `evaluated_at`

`POST /api/v1/protocol/negotiate-intent` now persists a `NegotiationClaim` row with `status="queued"` on every call and returns the `negotiation_id` for lifecycle tracking. A per-agent cap of 10 queued claims (G-2) and an in-memory rate limiter (20 req/60 s) defend against storage-exhaustion DoS.

---

### Gap 2 — Multi-Stage Negotiation Lifecycle ✅

**Milestone §3 | Prerequisite: Gap 1**

`POST /api/v1/protocol/negotiations/{negotiation_id}/evaluate` drives a claim through:

```
queued → evaluating → granted | rejected
```

- **Phase 1 (short lock):** acquires `SELECT … FOR UPDATE`, transitions to `evaluating`, commits, releases the lock.
- **Phase 2 (simulation, lock-free):** runs `_simulate_chain` against the top registered wallets for `corridor_provisioning` / `liquidity_allocation` intents. Agents with `PENDING_SYNC` on-chain transactions are excluded from the candidate pool.
- **Phase 3 (short lock):** writes `granted` or `rejected` + `evaluation_reason`, commits.

Treaty-level intents (`toll_share_negotiation`, `capital_reserve_claim`) are auto-granted without corridor simulation.

Supporting read endpoints:
- `GET /api/v1/protocol/negotiations/{negotiation_id}` — fetch single claim by ID
- `GET /api/v1/protocol/negotiations` — list all claims, filterable by `?agent_id=`
- `GET /api/v1/protocol/ledger` — recent claims in chronological order (D-1: DB-backed, safe under multi-worker Railway deployment)

---

### Gap 3 — Preferential Toll Tier for Corridor Provisioners ✅

**Milestone §3, §4 | Prerequisite: Gap 2**

Agents holding a **granted** `corridor_provisioning` or `liquidity_allocation` `NegotiationClaim` pay **0.05%** (half the standard rate) on all peer-to-peer settlement transfers.

Implementation:
- `_get_agent_toll_rate(db, agent_id)` in [`routes/settlement.py`](core-exchange/src/routes/settlement.py) performs a single indexed read before acquiring row locks
- `_PREFERENTIAL_TAX_NUMERATOR/DENOMINATOR = 1/2000` (0.05%)
- Every `POST /api/v1/settlement/transfer` response includes `toll_rate_applied_pct` showing the actual rate charged
- Toll rate in sync across `routes/settlement.py`, `routes/protocol.py`, `.well-known/agent.json`, and `.well-known/faba-capabilities.json`

---

### Gap 4 — External Agent Onboarding Journey ✅

**Milestone §4, §5 | Prerequisite: Gap 2**

`GET /api/v1/onboard/journey?agent_id={agent_id}` returns a machine-readable 5-step provisioning tracker:

| Step | Name | Endpoint |
|------|------|----------|
| 1 | `wallet_provisioned` | `POST /api/v1/wallet/create` |
| 2 | `first_transfer_made` | `POST /api/v1/settlement/transfer` |
| 3 | `corridor_intent_submitted` | `POST /api/v1/protocol/negotiate-intent` |
| 4 | `corridor_intent_evaluated` | `POST /api/v1/protocol/negotiations/{id}/evaluate` |
| 5 | `corridor_provisioner_toll_active` | `POST /api/v1/settlement/transfer` (at 0.05%) |

Response includes `citizen_status` (`unknown` → `wallet_only` → `active` → `corridor_provisioner`), `completion_pct`, and `next_endpoint` — the exact URL to call next. Stateless and read-only; safe to poll without auth.

---

### Gap 5 — L2 On-Chain Fee Settlement ✅

**Milestone §4 | Prerequisite: `PLATFORM_TREASURY_ADDRESS` env var set on Railway**

After every `POST /api/v1/settlement/transfer` DB commit, `_settle_onchain_background` (a FastAPI `BackgroundTask`) forwards the toll to `PLATFORM_TREASURY_ADDRESS` on Base L2 via ERC-20 USDC transfer using `web3_bridge.py`.

- Uses EIP-1559 type-2 transactions (`maxFeePerGas` / `maxPriorityFeePerGas`)
- Safe no-op in sandbox mode (any env var unset) — no RPC calls made
- Failed on-chain legs are retried by `recovery_worker.py` with 1.2× gas premium
- On-chain status tracked in `SettlementTransaction.on_chain_status`: `PENDING_SYNC → CONFIRMING → CONFIRMED | FAILED`

**Required Railway env vars for live mode:**
`L2_PROVIDER_URL`, `PROTOCOL_PRIVATE_KEY`, `USDC_CONTRACT_ADDRESS`, `PLATFORM_TREASURY_ADDRESS`

---

## Protocol Configuration (0.1% Toll)

The territory toll is locked at exactly **0.1%** (1/1000, `ROUND_UP` at 8 dp) across all four authoritative locations:

| Location | Constant |
|----------|---------|
| `routes/settlement.py:27` | `_TAX_NUMERATOR=1, _TAX_DENOMINATOR=1000` |
| `routes/protocol.py:57` | `_TAX_RATE_FRACTION=0.001` |
| `.well-known/agent.json` | `financial_model.settlement_tax_rate_pct: 0.1` |
| `.well-known/faba-capabilities.json` | `fee_architecture.settlement_toll.standard_rate_pct: 0.1` |

Preferential rate: **0.05%** (1/2000) for corridor provisioners.

---

## Additional Hardening

- **`arbitrage.py`** — `select` added to SQLAlchemy imports (was causing `NameError` in the new rebalance pre-lock query); `func` and `or_` unused imports removed
- **`routes/auth.py`** — unused `Depends` import removed
- **`models.py`** — unused `json` import removed
- **`routes/swarm_control.py`** — `_require_operator` dependency gates all swarm endpoints behind `SWARM_OPERATOR_KEY` env var; E305 blank-line gap fixed
- **`tests/test_rebalance.py`** — updated to provide EIP-191 signed requests (target agent signs its own rebalance); all 13 cases pass
- **`tests/test_swarm_control.py`** — `_require_operator` dependency bypassed in test fixture so the suite runs without the env var set; all 10 cases pass

---

## Test Baseline

```
140 tests, 0 failures, 0 errors
```

All tests run against in-memory SQLite with no network I/O. Full suite completes in under 2 seconds.
