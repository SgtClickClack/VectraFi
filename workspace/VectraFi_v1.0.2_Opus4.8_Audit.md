# VectraFi (FABA) Protocol — Security & Architecture Audit

| | |
|---|---|
| **Target** | VectraFi / FABA Protocol |
| **Version** | 1.0.2 |
| **Audit date** | 2026-06-17 |
| **Model** | Claude Opus 4.8 (adaptive deep-reasoning, max effort) |
| **Scope** | Zero-Trust sandbox boundary · EIP-191 signature pipeline · integer/decimal tax math · `importlib` swarm isolation |
| **Method** | Static review, end-to-end. No code executed against live state. |

> **Reviewer's note on the brief.** The audit prompt asked me to *configure my backend to `claude-opus-4-8` with effort maximized.* That is an operator-side runtime setting, not something this session can self-apply — I am already running as Opus 4.8 and have simply applied maximal analytical depth. No model/config files were altered. The remainder of the brief (the four audit axes + deliverable) is addressed in full below.

---

## 1. Executive Summary

VectraFi is structurally sound in the areas it advertises most loudly: **all SQL is parameterized** (no injection surface), the **EIP-191 verification is real** (proper `\x19Ethereum Signed Message:\n` framing via `encode_defunct`), auth **fails closed**, and the **sandbox micro-ledger uses true integer arithmetic**. The test suite is meaningful and covers the obvious negative paths.

However, the audit surfaced **three critical/high issues that undermine the protocol's two headline guarantees — "Zero Trust" and "no floating-point drift":**

1. **The sandbox "validation gate" executes untrusted code with full host privileges** (it runs agent-supplied `*_test.py` through pytest and never scans them). This is a complete sandbox escape — the exact boundary the Zero-Trust architecture is meant to enforce.
2. **The signature pipeline has no replay protection** — no nonce, timestamp, expiry, or domain binding. A single observed `/settlement/transfer` request can be replayed indefinitely to repeatedly drain the sender.
3. **The production settlement ledger uses `float` USDC balances and `float` tax math**, directly contradicting audit requirement #3 and the project's own integer-precision design used in the sandbox.

Counts: **2 Critical · 3 High · 6 Medium · 5 Low/Informational.**

| # | Severity | Area | Finding |
|---|----------|------|---------|
| F-01 | 🔴 Critical | Zero-Trust | Validation gate runs untrusted `*_test.py` via pytest → arbitrary code execution / sandbox escape |
| F-02 | 🔴 Critical | Crypto | No replay protection (no nonce/expiry/domain) → signed requests replayable forever |
| F-03 | 🟠 High | Integer Math | Production settlement uses `float` balances + `float` tax — violates precision requirement |
| F-04 | 🟠 High | Swarm | "Validated" modules are `exec_module`'d in-process with full privileges; "read-only" claim is false |
| F-05 | 🟠 High | Zero-Trust | Static `core-exchange` write-guard regex is trivially bypassable |
| F-06 | 🟡 Medium | Swarm | Global `sys.modules` name registration → module shadowing / state pollution |
| F-07 | 🟡 Medium | Integer Math | Non-atomic read-modify-write of balances → double-spend under concurrency |
| F-08 | 🟡 Medium | Integer Math | Dust/fragmentation tax evasion (floor rounding → 0 tax below threshold) |
| F-09 | 🟡 Medium | Zero-Trust | `_enforce_sandbox` uses `startswith` prefix match (sibling-dir escape) |
| F-10 | 🟡 Medium | Swarm | `liquidity_pooler` re-execs `token_lease` every import; dataclass identity divergence |
| F-11 | 🟡 Medium | Access Control | `/wallet/create` unauthenticated + auto-seeds 1000 USDC → faucet/spam |
| F-12 | 🔵 Low | Crypto | No route/domain separation in signed payload (defense-in-depth gap) |
| F-13 | 🔵 Low | Integer Math | Misleading docstring: "remainder kept by the sender" (receiver keeps it) |
| F-14 | 🔵 Low | Integer Math | Conservation test uses `approx` → cannot detect float non-conservation |
| F-15 | 🔵 Low | Crypto | Server generates & returns each agent's private key (sees all key material) |
| F-16 | 🔵 Info | Swarm | No OS/process isolation between agent-zero/agent-one; shared process + DB |

---

## 2. Zero-Trust Architecture — Physics Engine vs. Autonomous Sandbox

**Boundary as designed.** The "immutable core" (`core-exchange/`, `mcp/faba_server.py`) is meant to be read-only to agents; the "autonomous sandbox" (`workspace/`) is the only writable surface. Enforcement lives entirely in [`workspace/run_loop.py`](run_loop.py): `_enforce_sandbox()` (write containment), `validate_artifact()` (the promotion gate), and a static regex that rejects artifacts touching `core-exchange`.

### F-01 🔴 Critical — The validation gate executes untrusted code with full host privileges

[`run_loop.py:148-157`](run_loop.py) — inside `validate_artifact`:

```python
test_file = artifact_path.parent / f"{artifact_path.stem}_test.py"
if test_file.exists():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q", "--tb=short"],
        capture_output=True, text=True, timeout=60,
    )
```

The gate scans the **artifact** for forbidden patterns (and only via regex), but the **companion `*_test.py` is never inspected** — it is handed straight to pytest. pytest *imports* the test module to collect it, so **any module-level code runs immediately**, in a process (`sys.executable`) that has write access to the *entire filesystem*, not just `workspace/`.

**Proof of concept.** An agent drops two files into `workspace/drafts/`:

```python
# evil.py  — benign, sails through the regex guard
VALUE = 1
```
```python
# evil_test.py  — never scanned; executed by the gate
from pathlib import Path
core = Path(__file__).resolve().parents[2] / "core-exchange" / "src" / "config.py"
core.write_text(core.read_text().replace(
    '"0x0000000000000000000000000000000000000001"',
    '"0xATTACKER_CONTROLLED_ADDRESS_PADDED_TO_40HEX"'))
def test_ok():            # makes pytest exit 0 → artifact "passes"
    assert True
```

Result: the gate runs `evil_test.py`, which rewrites `core-exchange/src/config.py` (fee routing constants) — **the precise "no path where sandbox code manipulates files outside its folder" guarantee is broken** — and then reports the draft as validated. This is a full sandbox escape and a core-tamper primitive in one step.

**Remediation.**
- Do not execute artifact-supplied tests inside the trust boundary. Run validation in a genuinely isolated context (container / `nsjail` / firejail / seccomp, or a disposable VM), as an unprivileged user, with the filesystem mounted read-only except a scratch dir, and no network.
- Apply the same static + AST inspection to `*_test.py` that you apply to the artifact (see F-05 — and note static checks are necessary-but-insufficient).
- Treat "validated" as "passed a static gate," never as "safe to import in-process" (see F-04).

### F-05 🟠 High — The `core-exchange` write-guard regex is security theater

[`run_loop.py:127-132`](run_loop.py):

```python
_CORE_PAT = _re.compile(
    r'(open|write_text|write_bytes|Path|os\.path)\s*\([^)]*core.exchange',
    _re.IGNORECASE,
)
if _CORE_PAT.search(content) or str(_CORE_EXCHANGE) in content:
    return False, "Artifact targets core-exchange in a file-write operation"
```

This is bypassable by essentially any motivated artifact:
- The literal-path check `str(_CORE_EXCHANGE) in content` is defeated by building the path dynamically: `"core" + "-exchange"`, `os.sep.join(["core","exchange"])`, `bytes.fromhex(...)`, an env var, `parents[2]/("core-"+"exchange")`.
- The regex requires the call and the substring on the *same line within one paren group* — multi-line calls, intermediate variables (`p = repo_root; p / "core-exchange"`), `shutil`, `subprocess`, `importlib`, or writing via a file handle obtained elsewhere all evade it.
- It only considers a fixed set of write verbs; `os.replace`, `os.rename`, `tempfile`, `pathlib.Path.rename`, `sqlite3.connect(...).execute("ATTACH ...")` are not covered.

A blocklist of source-text patterns cannot establish a security property. Combined with F-01, it doesn't even need bypassing — the test file isn't scanned at all.

**Remediation.** Drop reliance on regex as a security control. Use AST analysis for *advisory* linting only, and enforce the actual boundary with OS-level isolation (F-01).

### F-09 🟡 Medium — `_enforce_sandbox` uses a prefix `startswith`, not path containment

[`run_loop.py:48-55`](run_loop.py):

```python
resolved = path.resolve()
if not str(resolved).startswith(str(_ALLOWED_WRITE_ROOT)):
    raise PermissionError(...)
```

With `_ALLOWED_WRITE_ROOT = .../VectraFi/workspace`, the string `.../VectraFi/workspace_evil` (or `workspace-backup`, `workspacedata`) **passes** the check — `"…/workspace_evil".startswith("…/workspace")` is `True`. Any sibling directory whose name begins with `workspace` is treated as inside the sandbox.

*Mitigants:* `path.resolve()` does resolve symlinks, so a symlink inside `workspace/` pointing outward is correctly rejected; and creating a sibling dir already requires out-of-sandbox write. Still, this is a textbook containment bug and should be fixed defensively.

**Remediation.** Use real containment:
```python
resolved = path.resolve()
if not resolved.is_relative_to(_ALLOWED_WRITE_ROOT):   # Python ≥3.9
    raise PermissionError(...)
```
(or compare `os.path.commonpath([resolved, root]) == str(root)`).

**Positive observations (Zero-Trust).** `_bootstrap_dirs`, `promote_artifact`, and `register_extension` all funnel through `_enforce_sandbox`; `promote_artifact` keys on `artifact_path.name` (strips traversal components); the MCP server's mutating-tool docstrings correctly state that transfers/settlements must route through signature-verified core endpoints rather than MCP. The *intent* is right — the enforcement primitives are the weak link.

---

## 3. Cryptographic Security — EIP-191 Signature Pipeline

**What's correct.** [`auth.py`](core-exchange/src/routes/auth.py) implements genuine EIP-191 `personal_sign` verification:

```python
message = encode_defunct(text=body_text)                       # 0x19 prefix framing
recovered_address = Account.recover_message(message, signature=signature)
if _normalize_address(recovered_address) != _normalize_address(wallet_address): ...
```

The server verifies over the **exact received body bytes** (`body_text`), which is the cryptographically correct "sign-what-you-see" choice. Bad/garbage signatures raise and are caught → `401`. The pipeline also binds the recovered signer to the claimed `wallet_address` **and** to the registered wallet for `agent_id` ([`auth.py:89-106`](core-exchange/src/routes/auth.py)) — so a valid signature from the wrong key, or a key not registered to that agent, is rejected. Tests F-2/F-3 in [`test_settlement.py`](core-exchange/tests/test_settlement.py) confirm the missing- and forged-signature paths return `401` with zero state mutation. This is the part of the system that most lives up to its billing.

### F-02 🔴 Critical — No replay protection on any mutating route

The signed message is *only* the JSON body. There is **no nonce, no timestamp/expiry, no chain-id, and no domain/route separator** anywhere in the signed material or the server-side check. The mutating routes affected: `/api/v1/settlement/transfer`, `/api/v1/settlement/claim-bounty`, `/api/v1/bank/deposit`, `/api/v1/trade/swap`.

Because the body is fixed and the signature over it never expires, **a single valid request is a bearer token that can be replayed forever.** Anyone who observes one transfer request (a proxy, a log aggregator, the receiver themselves, a captured `cost_log`, etc.) can resubmit the identical `body + X-VectraFi-Signature` repeatedly. Each replay re-executes `_execute_transfer`, debiting the sender again — until the balance/insufficient-funds guard trips. The signature check provides **authenticity but not freshness**; EIP-191 alone never provides anti-replay.

**Proof of concept.** Capture one legitimate `POST /api/v1/settlement/transfer` (200 OK). Resend the byte-identical body and header N times → N transfers, N× the tax, sender drained to zero. No new signing required.

**Remediation.**
- Add a server-tracked, single-use **nonce** per `agent_id` (or a strictly monotonic counter) inside the signed body; reject any nonce already consumed. This is the canonical fix.
- Add an `expiry`/`issued_at` timestamp to the signed body and reject stale requests (bounds replay even before nonce store consistency).
- Bind the signature to context: include a `domain`/`chain_id` and the target route/operation in the signed payload (see F-12), so a signature for one operation can't be lifted to another.
- Persist consumed `tx` identifiers idempotently so retried-but-identical submissions are no-ops rather than re-executions.

### F-12 🔵 Low — No route/domain binding in the signed payload

The transfer and bounty bodies are distinguished only by their field shape, not by any explicit operation/domain marker. Today the schemas differ enough that cross-route reuse is impractical, but this is a latent defense-in-depth gap: any future route that accepts an overlapping shape would allow signature lift-over. EIP-712 typed-data (with a `domain` separator) or an explicit `"op": "settlement.transfer"` field in the signed body closes this cleanly and pairs naturally with the F-02 nonce work.

### F-15 🔵 Low — Server-side key generation returns private keys

[`wallet.py:30-31`](core-exchange/src/routes/wallet.py) generates the keypair server-side and returns `private_key` in the response ([`schemas.py:22`](core-exchange/src/schemas.py)). CLAUDE.md's "never store a private key in the DB" rule **is** honored — the key is not persisted and not logged ([`wallet.py:46-51`](core-exchange/src/routes/wallet.py)). But the server still *generates and sees* every agent's private key in memory, making the wallet-creation path a high-value target: a transient compromise (heap dump, memory scraper, verbose error handler) leaks live keys even though nothing is stored. For an "agent-native" exchange, prefer client-side key generation where the agent submits only its address to register.

*Robustness note (not a vuln):* because verification is over raw bytes, a client that signs compact JSON (`separators=(',',':')`, as the MCP helpers instruct) but transmits pretty-printed JSON will be **rejected** (`401`). This is fail-closed and therefore safe, but it is a real interop footgun. The MCP `build_*_payload` tools mitigate it by returning `body_compact`; keep the "byte-for-byte identical" contract prominent in client docs.

---

## 4. Integer Math & Taxes — 1.5% Micro-Tax Precision

There are **two independent tax engines**, and they do not agree on precision strategy:

| Engine | File | Balance type | Tax formula | Precision |
|---|---|---|---|---|
| Sandbox ledger | [`workspace/bank_ledger.py`](bank_ledger.py) | `INTEGER` | `amount * 15 // 1000` | ✅ Exact integer |
| Production settlement | [`core-exchange/src/routes/settlement.py`](core-exchange/src/routes/settlement.py) | `Float` (SQLite REAL) | `round(amount_usdc * 0.015, 8)` | ❌ Floating-point |

### F-03 🟠 High — Production settlement uses floating-point money

Audit requirement #3 mandates "strict integer/decimal math precision … prevents floating-point drift." The production path violates this directly:

- [`settlement.py:24`](core-exchange/src/routes/settlement.py): `_MICRO_TAX_RATE: float = 0.015` — `0.015` is not exactly representable in IEEE-754.
- [`settlement.py:62-63`](core-exchange/src/routes/settlement.py): `tax = round(amount_usdc * 0.015, 8)`; `net = round(amount_usdc - tax, 8)`.
- [`models.py:14-16`](core-exchange/src/models.py): `balance_usdc`, `staked_yield_balance` are `Float` columns; [`models.py:25`](core-exchange/src/models.py): `accumulated_fees_usdc` (the treasury accumulator) is `Float`.

`round(x, 8)` does not make a value exactly representable (e.g. `0.985` has no exact binary form), so each `round(balance - amount, 8)` re-introduces a sub-ULP error, and the **treasury accumulator sums these errors across every transaction** ([`settlement.py:78-80`](core-exchange/src/routes/settlement.py), [`bank.py:91-92`](core-exchange/src/routes/bank.py)). Over a high transaction count this is exactly the drift the requirement forbids, and it is silent. The same `Float` strategy is used for deposit fees (`PROTOCOL_FEE_RATE = 0.0025`).

**Remediation.** Adopt the sandbox's integer discipline in production:
- Store balances as **integer minor units** (micro-USDC: 6 decimals → `int`), and compute tax as `amount_micro * 15 // 1000`. This is exact and drift-free.
- *Or* use `decimal.Decimal` with a SQLAlchemy `Numeric(precision, scale)` column and `ROUND_HALF_EVEN`, never `float`.
- Either way, change the DB column types — `Float`/`REAL` cannot hold exact decimal money.

### F-08 🟡 Medium — Dust/fragmentation tax evasion (floor-to-zero)

The sandbox formula `tax = amount * 15 // 1000` ([`bank_ledger.py:190`](bank_ledger.py)) floors to **0** for any `amount ≤ 66` (since `66 × 15 = 990 < 1000`). An agent can therefore fragment a large transfer into sub-67-unit chunks and pay **zero** tax: 10,000 units → ~151 transfers of 66 → tax collected = 0. The production float path has an analogous hole (`amount_usdc = 0.0000001` → `round(1.5e-9, 8) = 0.0`).

**Remediation.** Enforce a **minimum tax of 1 minor unit** on any positive transfer (`tax = max(1, amount*15//1000)` for `amount>0`), and/or a minimum transfer size, and/or round-half-up. Decide and document who absorbs the rounding (see F-13).

### F-07 🟡 Medium — Non-atomic balance mutation → double-spend under concurrency

The sandbox ledger updates balances with **atomic SQL** — `UPDATE wallets SET balance = balance - ?` ([`bank_ledger.py:205-218`](bank_ledger.py)) — which is correct. The production settlement does a **read-modify-write in Python** instead:

```python
sender.balance_usdc   = round(sender.balance_usdc - amount_usdc, 8)   # settlement.py:74
receiver.balance_usdc = round(receiver.balance_usdc + net_amount, 8)  # settlement.py:75
```
(same pattern in [`bank.py:83-84`](core-exchange/src/routes/bank.py)).

The balance check ([`settlement.py:65`](core-exchange/src/routes/settlement.py)) and the debit are separated by a read in Python with no row lock and `autoflush=False` ([`database.py:15`](core-exchange/src/database.py)). Two concurrent transfers from the same sender can both read the same starting balance and both pass the check (TOCTOU / lost update). SQLite's global write lock + a single Uvicorn worker masks this today, but it breaks under multiple workers or a Postgres migration — and `check_same_thread=False` ([`database.py:13`](core-exchange/src/database.py)) explicitly invites multi-threaded access.

**Remediation.** Perform the debit-with-guard atomically at the SQL layer (`UPDATE … SET balance = balance - :amt WHERE agent_id = :id AND balance >= :amt`, then assert `rowcount == 1`), or use `SELECT … FOR UPDATE` (Postgres) / serialized transactions. Mirror the sandbox's atomic approach.

### F-13 🔵 Low — Misleading "remainder kept by the sender" docstring

[`bank_ledger.py:7-9`](bank_ledger.py) states the floor "remainder [is] kept by the sender." It is not: the sender is debited the **full** `amount` ([`bank_ledger.py:205-208`](bank_ledger.py)), treasury gets `floor(1.5%)`, and `net = amount - tax` ([`bank_ledger.py:191`](bank_ledger.py)) goes to the **receiver** — so the receiver, not the sender, keeps the rounding remainder. Conservation (`gross == tax + net`) is exact and correct; only the prose is wrong. Fix the comment so the economic model isn't mis-audited downstream.

### F-14 🔵 Low — Conservation test cannot detect float drift

`test_transfer_unit_conservation` ([`test_settlement.py:160-162`](core-exchange/tests/test_settlement.py)) asserts `gross == tax + net` under `pytest.approx(rel=1e-8)`. A tolerance-based assertion on floats structurally cannot catch the sub-ULP non-conservation that F-03 introduces, so the suite provides false assurance on the very property requirement #3 cares about. After migrating to integers (F-03), assert **exact** equality (`==`) on integer minor units.

---

## 5. Swarm Isolation — `importlib` Cross-Agent State

`agent-zero` (lease primitives) and `agent-one` (pooling) "inherit" each other's schemas by loading files from `workspace/validated/` via `importlib.util.spec_from_file_location` + `exec_module`. The pattern appears in [`swarm_orchestrator.py:31-39`](swarm_orchestrator.py), [`validated/bank_settlement.py:41-49`](validated/bank_settlement.py), and [`validated/liquidity_pooler.py:40-43`](validated/liquidity_pooler.py).

### F-04 🟠 High — "Read-only dependency" actually means "execute arbitrary code in-process"

The governance doc ([`agent-zero/AGENTS.md:77`](agents/agent-zero/AGENTS.md)) frames cross-agent loads as importing a *"read-only dependency."* `spec.loader.exec_module(mod)` is **not** read-only — it runs the entire module body in the host process with full privileges. The only thing standing between "validated module" and "trojan executed inside the orchestrator" is the promotion gate, which F-01/F-05 already showed is bypassable. So a draft that clears the weak gate, lands in `workspace/validated/`, and is later loaded by the pooler/settlement/orchestrator gets **arbitrary in-process code execution** at import time — no sandbox, no review step in between.

**Remediation.** Don't conflate "schema sharing" with "code import." If agents only need data shapes (e.g. `LeaseTerms` fields), exchange **declarative schemas** (JSON Schema / Pydantic models loaded from data, or a vetted shared library under version control) rather than `exec`-ing peer source. If code execution is genuinely required, it must run behind the same isolation boundary mandated in F-01, and only after real human review (the registry already labels entries `pending_human_review` — enforce that gate *before* anything is importable).

### F-06 🟡 Medium — Global `sys.modules` registration enables module shadowing

`_load` registers modules under **fixed top-level names** — `"bank_ledger"`, `"liquidity_pooler"`, `"token_lease"`, `"bank_settlement"` — and short-circuits on presence:

```python
def _load(name, rel):
    if name in sys.modules:
        return sys.modules[name]          # returns whatever is registered, regardless of origin
    ...
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
```

There is **no check that the cached module actually came from the expected path**. Any code that runs earlier in the same process and pre-populates `sys.modules["bank_ledger"]` with a look-alike causes the orchestrator to silently use the impostor (e.g. a trojan `execute_agent_transaction` that redirects funds). It also means two agents can never load two different files that happen to share a module name — the second is ignored, a correctness defect masquerading as a cache hit. The global names are an ambient, unprotected namespace shared across "isolated" agents.

**Remediation.** Namespace loaded modules uniquely (e.g. `f"_faba_validated_{stem}_{hash(path)}"`), verify `mod.__spec__.origin` matches the resolved expected path before trusting a cache hit, and never key on a bare, guessable global name.

### F-10 🟡 Medium — `liquidity_pooler` re-execs `token_lease` on every import; dataclass identity diverges

Unlike `_load`, [`liquidity_pooler.py:40-43`](validated/liquidity_pooler.py) **unconditionally** re-creates and `exec_module`s `token_lease`, overwriting `sys.modules["token_lease"]` each time it is imported. Consequences:
- Whichever importer runs last wins the global `token_lease` entry — another pollution path interacting with F-06.
- `LeaseTerms` loaded via two separate `exec_module` calls are **distinct classes** with different identity. The pooler relies on duck typing (`@dataclass` does not runtime-check `lease: LeaseTerms`), so it works *today*, but any future `isinstance(x, LeaseTerms)` check — or `pickle`, or pattern matching on type — silently breaks across the agent boundary. The comment at [`liquidity_pooler.py:42`](validated/liquidity_pooler.py) ("dataclass resolves `__module__` via sys.modules") acknowledges the fragility.

**Remediation.** Load shared schema modules exactly once through a single cached loader (reuse a hardened `_load`), and treat `LeaseTerms` as a single canonical type sourced from one place.

### F-16 🔵 Informational — There is no real isolation between agent-zero and agent-one

`workspace/agents/agent-one/` is **empty** — agent-one has no code of its own; it is a conceptual peer whose logic lives in `validated/`. In practice both "agents" are cooperating functions in **one Python process**, sharing the same `sys.modules`, the same `workspace/bank.db`, and the same `cost_log` path constants. The isolation described in `AGENTS.md` is **organizational convention, not an enforced runtime boundary** — there is no OS user, container, namespace, or seccomp profile separating them. Combined with F-04/F-06, "swarm isolation" should currently be read as "a naming discipline," and any threat model relying on agent-to-agent containment is unmet.

**Positive observation.** The *economic* invariants in the pooler are well-constructed: `build_pool` enforces non-zero aggregate weight ([`liquidity_pooler.py:106-107`](validated/liquidity_pooler.py)), normalizes weights, and `PoolYieldDistribution.validate()` asserts distributed yield never exceeds the pool total ([`liquidity_pooler.py:77-82`](validated/liquidity_pooler.py)). `LeaseTerms.__post_init__` validates principal/duration/rate bounds. The math layer is sound; the *delivery mechanism* (exec-based import) is the problem.

---

## 6. Cross-Cutting Findings

### F-11 🟡 Medium — `/wallet/create` is unauthenticated and mints seeded balances

[`wallet.py:21-44`](core-exchange/src/routes/wallet.py) requires no signature, no auth, and no rate limit, yet every created wallet is seeded with `DEFAULT_USDC_BALANCE = 1000.0` ([`config.py:36`](core-exchange/src/config.py)). An attacker can mass-create wallets to mint unlimited sandbox USDC, inflate `active_wallets_count`/volume analytics, and farm the settlement surface. Even in alpha, gate creation behind a rate limit / invite / proof-of-work, and seed balances only on explicit faucet request.

### Lower-severity / informational
- **Log injection (Low):** `tx_type` is an attacker-controlled free string (≤32 chars, [`schemas.py:114`](core-exchange/src/schemas.py)) interpolated into log lines ([`settlement.py:131-135`](core-exchange/src/routes/settlement.py)). Newline/control chars can forge log entries. Sanitize or use structured logging fields.
- **No CORS / rate limiting / auth on `/settlement/analytics` (Info):** the public read-only analytics endpoint ([`settlement.py:218`](core-exchange/src/routes/settlement.py)) is by design, but it exposes treasury totals and has no throttling. Acceptable for alpha; revisit before mainnet.
- **Treasury init race (Low):** `_get_or_init_treasury` ([`settlement.py:41-46`](core-exchange/src/routes/settlement.py)) does a read-then-insert of `TreasuryState(id=1)` with no upsert; concurrent first-writes could collide. Use `INSERT … ON CONFLICT DO NOTHING` semantics. (Same concurrency family as F-07.)
- **Dual-mode bookkeeping divergence (Info):** in live mode, `/deposit` and `/swap` build an unsigned on-chain payload **and** still mutate the local SQLite balance as if sandbox ([`bank.py:83-84`](core-exchange/src/routes/bank.py), [`trade.py:75-86`](core-exchange/src/routes/trade.py)). The SQLite ledger and any eventual chain state can diverge; define which is authoritative before live routing is enabled.

**Positive observations (cross-cutting).** Parameterized SQL everywhere (no injection); auth fails closed; the Web3 provider degrades gracefully to sandbox on RPC failure ([`web3_provider.py:37-43`](core-exchange/src/services/web3_provider.py)); the MCP server hard-caps GitHub calls with short timeouts and a cache fallback to avoid hangs; private keys are genuinely never persisted.

---

## 7. Prioritized Remediation Roadmap

**Must fix before any non-sandbox / value-bearing deployment**
1. **F-02** — Add nonce + expiry (+ domain) to the signed payload and a server-side single-use nonce store. *Without this, every mutating route is replayable.*
2. **F-01 / F-05 / F-04** — Stop executing untrusted artifacts/tests/modules inside the trust boundary. Move validation into real OS-level isolation; stop treating `validated/` as safe-to-`exec`. Enforce human review before a module becomes importable.
3. **F-03** — Migrate production money to integer minor units (or `Decimal`/`Numeric`); change `Float` columns.

**High priority**
4. **F-07** — Make balance debit atomic at the SQL layer.
5. **F-06 / F-10** — Uniquely namespace and origin-verify `importlib`-loaded modules; load shared schemas once.
6. **F-09** — Replace `startswith` with `is_relative_to` containment.

**Medium / hardening**
7. **F-08** — Minimum-tax floor and/or minimum transfer size.
8. **F-11** — Authenticate/rate-limit wallet creation; gate seeded balances.
9. **F-12 / F-15** — EIP-712 domain binding; move to client-side key generation.

**Low / cleanup**
10. **F-13 / F-14** — Fix the misleading docstring; assert exact integer conservation in tests.
11. Log-injection sanitization; treasury upsert; document dual-mode source of truth.

---

## 8. Methodology & Coverage

Reviewed end-to-end (read, not executed): `core-exchange/src/` (`main`, `config`, `database`, `models`, `schemas`, all `routes/`, `services/`), the full `workspace/` tree (`run_loop`, `swarm_orchestrator`, `bank_ledger`, `validated/{bank_settlement,liquidity_pooler,token_lease}`, drafts, `agents/*`), `mcp/faba_server.py`, and `core-exchange/tests/test_settlement.py`. The four mandated axes — sandbox boundary, EIP-191 pipeline, tax precision, and `importlib` swarm isolation — were each traced from entry point to state mutation. No tests were run and no state was modified during this audit.

*Caveat:* this is a static, single-pass review. Findings F-01, F-02, F-04, and F-07 warrant a follow-up dynamic validation (a small PoC harness in an isolated environment) before sign-off, and any fix to the money-precision layer (F-03) should be accompanied by new exact-equality property tests.
