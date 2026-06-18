#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR/core-exchange/src"

echo "============================================"
echo "  VectraFi Production Swarm Launcher"
echo "============================================"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting preflight check..."
echo "  API target : ${VECTRAFI_API_URL:-http://127.0.0.1:8000 (local fallback)}"
echo "  Dry-run    : ${SWARM_DRY_RUN:-0}"
echo ""

if ! python "$SRC_DIR/web3_bridge.py" --test-rpc; then
    echo ""
    echo "[ERROR] RPC preflight failed. Swarm launch aborted."
    echo "        Verify the following Railway environment variables are set:"
    echo "          - L2_PROVIDER_URL"
    echo "          - PROTOCOL_PRIVATE_KEY"
    echo "          - USDC_CONTRACT_ADDRESS"
    echo "          - PLATFORM_TREASURY_ADDRESS"
    echo ""
    exit 1
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Preflight passed. Launching live swarm..."
echo ""

SWARM_DRY_RUN=0 python "$SRC_DIR/seed_swarm.py"

echo ""
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Swarm session complete."
