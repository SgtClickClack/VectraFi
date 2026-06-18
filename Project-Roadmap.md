# VectraFi Project Roadmap
> Cross-reference of `VectraFiBusinessPlan.html` milestones against the live codebase.
> Source of truth for alignment checks before proposing new code or infrastructure.
> Updated: 2026-06-19

---

## How to use this document

Before writing any new code, answer three questions:
1. **Does this milestone already have a primitive that covers the need?** (see each section's "Existing primitives" list)
2. **Does the change advance a `⚠️ Gap` item?** If yes, proceed.
3. **Does the change deviate from the Agentic Sovereign Territory / 0.1% toll model?** If yes, flag it and propose a territory-first alternative.

---

## Milestone 1 — Executive Summary: Agentic Sovereign Territory

**Spec goal:** Establish VectraFi as a sovereign jurisdiction for autonomous agents — not a product, a territory. Revenue model is a 0.1% frictionless territory toll across M2M micro-transaction volume.

| Status | Item | Code location |
|--------|------|---------------|
| ✅ | 0.1% territory toll (exact 1/1000, ROUND_UP, dust floor) | `routes/settlement.py:27-58` |
| ✅ | Agent-native API (no human frontend required) | `main.py` — 9 raw API routers |
| ✅ | Platform Treasury accumulates toll revenue | `models.py:TreasuryState` |
| ✅ | Three founding citizen agents (Alpha, Beta, Gamma swarm desks) | `seed_swarm.py` |
| ✅ | Zero-storage private key policy (returned once, never persisted) | `routes/wallet.py` |

**Existing primitives that cover this milestone:** `_execute_transfer`, `TreasuryState`, `AgentWallet`.

---

## Milestone 2 — Architectural Primitives: The Bedrock

**Spec goal:** Lean, high-throughput Python footprint. Four concrete primitives: alphabetical row locking, fault-resilient recovery, non-mutating dry-run simulation, streaming telemetry.

| Status | Item | Code location |
|--------|------|---------------|
| ✅ | Pessimistic alphabetical row locking (SELECT…FOR UPDATE, sorted key order) | `routes/settlement.py:109-119` |
| ✅ | TreasuryState locked after wallets — prevents lost-update race | `routes/settlement.py:144-153` |
| ✅ | Fault-resilient PENDING_SYNC recovery (1.2× gas premium) | `recovery_worker.py` |
| ✅ | EIP-1559 type-2 tx support in recovery | `web3_bridge.py` |
| ✅ | Non-mutating path simulation via `db.begin_nested()` savepoints | `routes/arbitrage.py` |
| ✅ | Streaming telemetry dashboard (2.5 s auto-scroll, regex log parsing) | `templates/index.html` |
| ✅ | Test baseline — 71 tests, 0 failures | `core-exchange/tests/` |

**Before adding any new concurrency primitive:** check whether alphabetical row locking already covers the collision scenario. It prevents all AB/BA deadlocks on wallet pairs — do not introduce alternative locking schemes.

**Before adding a new error-recovery path:** check whether `recovery_worker.py` can be extended (e.g. new `tx_type` filter) rather than creating a parallel retry mechanism.

---

## Milestone 3 — Evolutionary Model: Agent-Built Territory

**Spec goal:** Territory evolves because citizen agents have a sovereign economic interest in doing so. Two mechanisms: autonomous liquidity rebalancing, and a negotiation layer where agents claim resource allocations.

| Status | Item | Code location |
|--------|------|---------------|
| ✅ | Dynamic liquidity rebalancing (volume × 0.5% safety floor) | `seed_swarm.py` |
| ✅ | Multi-hop rebalancing sweeps across idle desks | `seed_swarm.py` + `routes/arbitrage.py` |
| ✅ | MCP negotiation entry point (`POST /api/v1/protocol/negotiate-intent`) | `routes/protocol.py:60-84` |
| ✅ | Protocol params endpoint (agents read toll rate, hops, floors) | `routes/protocol.py:33-57` |
| ✅ | Negotiation state persistence — `NegotiationClaim` model, DB migration, GET by ID / list endpoints | `models.py:NegotiationClaim`, `routes/protocol.py` |
| ✅ | Multi-stage negotiation lifecycle (queued → evaluating → granted/rejected) | `routes/protocol.py:evaluate_negotiation` |
| ✅ | Preferential toll rate for corridor-provisioning agents | `routes/settlement.py:_get_agent_toll_rate`, `SettlementTransferResponse.toll_rate_applied_pct` |
| ⚠️ | `faba_server.py` standalone MCP server (spec §3 reference) | Spec intention fulfilled via REST; standalone MCP binary not built |

**Territory-first rule for §3 gaps:** Before building negotiation persistence, check if `routes/arbitrage.py:scan-paths` already surfaces underserved corridors that could seed negotiation claims. Before building toll tiers, confirm negotiation state is persisted first — a preferential toll has no anchor without a granted claim.

---

## Milestone 4 — Monetization & Token Economics

**Spec goal:** Phase 1 = closed-loop sandbox (internal swarm proves toll mechanics). Phase 2 = open territory (external agents provision their own corridors; volume scales by orders of magnitude).

| Status | Item | Code location |
|--------|------|---------------|
| ✅ | Phase 2 gate: `GET /api/v1/onboard/journey` — machine-readable 5-step provisioning tracker with citizen_status | `routes/onboard.py`, `schemas.py:OnboardingJourneyResponse` |
| ✅ | Phase 1 operational: swarm daemons accumulate toll in closed loop | `seed_swarm.py` + `models.py:TreasuryState` |
| ✅ | Toll rate locked at 0.1% — priced below rational competitor threshold | `routes/settlement.py:27` |
| ✅ | Deposit fee (0.25%, 80/20 treasury split) as secondary revenue stream | `routes/bank.py` |
| ✅ | Swap route (zero fee) as loss-leader for agent acquisition | `routes/trade.py` |
| ✅ | Phase 2 gate: external agent onboarding flow — machine-readable journey tracker with citizen_status tiers | `routes/onboard.py:GET /api/v1/onboard/journey`, `schemas.py:OnboardingJourneyResponse` |
| ✅ | L2 on-chain fee settlement — toll forwarded to `PLATFORM_TREASURY_ADDRESS` on Base L2 after each SQLite commit (live RPC mode); sandbox-safe no-op when env vars unset | `web3_bridge.py`, `routes/settlement.py:_settle_onchain_background` |

**Cross-reference check:** Any change to `_TAX_NUMERATOR` / `_TAX_DENOMINATOR` in `settlement.py` must be reflected in `routes/protocol.py:_TAX_RATE_FRACTION`, `core-exchange/src/.well-known/agent.json:financial_model.settlement_tax_rate_pct`, and `faba-capabilities.json`. These four must stay in sync.

---

## Milestone 5 — Go-To-Market & Discovery Strategy

**Spec goal:** Agents discover VectraFi by crawling directories and parsing machine-readable specs — not via human marketing. Publish OpenAPI, MCP server card, capability manifests, and agentic directory listings.

| Status | Item | Code location |
|--------|------|---------------|
| ✅ | OpenAPI spec auto-served at `/openapi.json` | FastAPI default + `generate_openapi.py` |
| ✅ | Swagger UI at `/docs` | FastAPI default |
| ✅ | Agent discovery card | `.well-known/agent.json` |
| ✅ | FABA capability manifest | `.well-known/faba-capabilities.json` |
| ✅ | MCP server card | `.well-known/mcp/server-card.json` |
| ✅ | Protocol manifesto (human-readable territory declaration) | `.well-known/manifesto.html` |
| ✅ | AGENTS_REGISTRY.md (public agent directory) | `AGENTS_REGISTRY.md` (root) |
| ✅ | StaticFiles mount for `.well-known` passive discovery | `main.py` |
| ⚠️ | Discovery broadcast script | `scripts/broadcast_discovery.py` (exists; confirm operational) |

**Before adding new discovery surfaces:** check whether the existing `.well-known/` layer or `agent.json` can be extended with a new field rather than creating a new file. Agent crawlers prefer fewer, richer files.

---

## Milestone 6 — Dual-Track Value Capture (HospoGo Parallel)

**Spec goal:** The same four constitutional primitives that govern VectraFi's financial territory map directly onto HospoGo's hospitality workforce marketplace — proving the architecture is a generalisable Agentic Territory pattern.

| VectraFi Primitive | VectraFi Location | HospoGo Application |
|--------------------|-------------------|----------------------|
| Alphabetical row locking | `routes/settlement.py:109` | Anti-race booking guard — prevent double-booking of shift-seeking professionals |
| `recovery_worker.py` | `recovery_worker.py` | Serverless hydration safeguard — recover dropped urgent shift requests |
| `db.begin_nested()` savepoints | `routes/arbitrage.py` | Pre-flight compliance router — validate award/Stripe/Xero before locking shift |
| MCP negotiation layer | `routes/protocol.py:60` | Autonomous shift routing — agents negotiate shift blocks and payout tiers |

**Note:** HospoGo implementation lives in a separate repository. When VectraFi primitives are extended, evaluate whether the extension also strengthens the corresponding HospoGo primitive before merging.

---

## Open Gaps (Priority Order)

| # | Gap | Milestone | Prerequisite |
|---|-----|-----------|--------------|
| ✅ | Negotiation state persistence (`NegotiationClaim` model + DB migration) | §3 | None |
| ✅ | Multi-stage negotiation lifecycle (queued → evaluating → granted/rejected) | §3 | Gap 1 ✅ |
| ✅ | Preferential toll tier for corridor-provisioning agents | §3, §4 | Gap 2 ✅ |
| ✅ | Phase 2 external agent onboarding journey — `GET /api/v1/onboard/journey` with 5-step progression and citizen_status tiers | §4, §5 | Gap 2 ✅ |
| ✅ | L2 on-chain fee settlement in production (PLATFORM_TREASURY_ADDRESS live) | §4 | Railway env var set |

---

## Deviation Flag Protocol

If a proposed feature does **not** advance one of the gaps above and does **not** extend an existing primitive, flag it with:

> **⛔ Territory Deviation:** This feature [description] does not map to VectraFiBusinessPlan.html §[N].
> **Territory-first alternative:** [suggestion that uses an existing primitive instead]

Examples of deviations to flag:
- Adding a 2% "premium" fee tier → deviates from §4 (0.1% is the strategic anchor; tiers belong in §3 toll negotiation, not settlement.py)
- Building a human-facing React frontend → deviates from §1 (territory is agent-native; human access is via telemetry dashboard only)
- Adding a centralised order book → deviates from §3 (agents provision corridors via negotiation, not a central matching engine)
