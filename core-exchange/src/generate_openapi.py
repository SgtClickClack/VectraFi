#!/usr/bin/env python3
"""
VectraFi OpenAPI schema generator.

Generates a complete, valid openapi.json from the live FastAPI application
schema. External LLM-driven agents can fetch this file to programmatically
map all execution paths, parameter schemas, and response models without
inspecting source code.

Usage:
    cd core-exchange/src
    python generate_openapi.py                    # writes openapi.json to CWD
    python generate_openapi.py --out /path/to/dir # writes to a specific directory
    python generate_openapi.py --stdout           # prints JSON to stdout

The generated schema includes all endpoints exposed by the VectraFi Core
Exchange, including:
  - Core execution rails: /api/v1/arbitrage/route-path, /rebalance, /scan-paths
  - Simulation queries: /api/v1/arbitrage/scan-paths
  - Protocol parameter inquiries: /api/v1/protocol/params
  - Live telemetry / liquidity depth: /api/v1/analytics/swarm, /treasury-breakdown
  - Settlement rails: /api/v1/settlement/transfer, /claim-bounty, /analytics
  - Banking: /api/v1/bank/deposit, /api/v1/trade/swap
  - Wallet management: /api/v1/wallet/create, /balance
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Bootstrap the module path so imports resolve from core-exchange/src/.
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from main import app  # noqa: E402 — path bootstrap must come first


def generate_schema() -> dict:
    """Return the full OpenAPI 3.x schema dict from the live FastAPI application."""
    return app.openapi()


def write_schema(out_dir: Path | None = None, stdout: bool = False) -> Path | None:
    schema = generate_schema()
    serialized = json.dumps(schema, indent=2, ensure_ascii=False)

    if stdout:
        print(serialized)
        return None

    target_dir = out_dir or Path.cwd()
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / "openapi.json"
    out_path.write_text(serialized, encoding="utf-8")
    print(
        f"[generate_openapi] Wrote {out_path} ({len(serialized):,} bytes)",
        file=sys.stderr,
    )
    return out_path


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Generate VectraFi OpenAPI schema")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="DIR",
        help="Output directory (default: current working directory)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print JSON to stdout instead of writing a file",
    )
    args = parser.parse_args()
    write_schema(out_dir=args.out, stdout=args.stdout)


if __name__ == "__main__":
    _cli()
