#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR/core-exchange/src"

echo "============================================"
echo "  VectraFi Production Swarm Launcher"
echo "============================================"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting preflight check..."

if ! python "$SRC_DIR/web3_bridge.py" --test-rpc; then
    echo ""
    echo "[ERROR] RPC preflight failed. Swarm launch aborted."
    echo "        Verify the following Railway environment variables are set:"
    echo "          - ETH_RPC_URL"
    echo "          - CHAIN_ID"
    echo "          - OPERATOR_ADDRESS"
    echo "          - VECTRAFI_SECRET_KEY"
    echo ""
    exit 1
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Preflight passed. Launching live swarm..."
echo ""

SWARM_DRY_RUN=0 python "$SRC_DIR/seed_swarm.py"

echo ""
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Swarm session complete."
