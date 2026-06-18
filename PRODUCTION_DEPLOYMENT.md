# VectraFi — Production Deployment (Base Sepolia → Base Mainnet)

## Switching from `sandbox` to `live_rpc`

Set all four variables in the Railway dashboard (**Settings → Variables**) and redeploy.

| Variable | Value |
|---|---|
| `L2_PROVIDER_URL` | `https://sepolia.base.org` |
| `PROTOCOL_PRIVATE_KEY` | `0x<64-hex-char gas-account private key>` |
| `USDC_CONTRACT_ADDRESS` | `<Base Sepolia USDC contract address>` |
| `PLATFORM_TREASURY_ADDRESS` | `<treasury wallet that receives the 1.5% tax leg>` |

The service reads these at startup. When all four are present `is_configured` returns `True` and `execution_mode` switches from `sandbox` to `live_rpc` (visible at `GET /health`).

---

## Verify connectivity before launching transfers

```bash
python core-exchange/src/web3_bridge.py --test-rpc
```

Expected output ends with `Result: OK — RPC endpoint reachable`. If it fails, check `L2_PROVIDER_URL` and network access from the Railway region.

---

## Launch the live swarm

```bash
# Against the Railway deployment
export SWARM_API_BASE="https://web-production-c8197.up.railway.app"
export SWARM_DRY_RUN="0"
export SWARM_POLL_MS="1000"
python core-exchange/src/seed_swarm.py
```

Or trigger via the REST API (server-side subprocess):

```bash
curl -X POST https://web-production-c8197.up.railway.app/api/v1/swarm/start \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false}'

# Status
curl https://web-production-c8197.up.railway.app/api/v1/swarm/status

# Stop
curl -X POST https://web-production-c8197.up.railway.app/api/v1/swarm/stop
```

---

## EIP-1559 transaction layer

Every on-chain settlement dispatches a **type-2 transaction** (Base L2 native):

```
maxFeePerGas        = baseFeePerGas × 2 + 1 gwei
maxPriorityFeePerGas = 1 gwei
```

The `_MAX_GAS_PRICE_GWEI = 500` ceiling is checked against `maxFeePerGas`. If exceeded the settlement is deferred to `PENDING_SYNC` rather than paying an unbounded gas cost.

The recovery worker (`recovery_worker.py`) retries `PENDING_SYNC` rows with a **1.2× premium** applied to `maxFeePerGas` to unstick mempool-lagging transactions.

---

## Telemetry endpoints (no auth required)

| Endpoint | Description |
|---|---|
| `GET /health` | Liveness + `execution_mode` |
| `GET /api/v1/settlement/analytics` | Fee accumulator, volume, wallet count |
| `GET /api/v1/analytics/treasury` | Treasury balance |
| `GET /api/v1/analytics/recent-transactions` | Last 10 settlements |
| `GET /api/v1/swarm/status` | Swarm liveness, PID, uptime |
