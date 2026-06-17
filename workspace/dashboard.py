#!/usr/bin/env python3
"""
VectraFi Protocol Telemetry Gateway — real-time terminal dashboard.

Polls three analytics endpoints concurrently and renders a flicker-free
ANSI dashboard using cursor-positioning rather than screen clears.

Endpoints polled:
  GET /api/v1/analytics/stats              — transaction volume, agents, rates
  GET /api/v1/analytics/treasury           — accumulated micro-tax fees (8dp)
  GET /api/v1/analytics/recent-transactions — rolling log of last 5 settlements

Target: VECTRAFI_TARGET_URL env var (default http://localhost:8000)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

# Force UTF-8 output so box-drawing characters render correctly on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# ANSI palette
# ---------------------------------------------------------------------------

ESC     = "\033["
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
RED     = "\033[91m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"
CYAN    = "\033[96m"
WHITE   = "\033[97m"

BG_DARK = "\033[40m"

# Cursor control
HOME    = "\033[H"
CLEAR   = "\033[2J"
CLREOL  = "\033[K"    # clear to end of line


def _mv(row: int, col: int) -> str:
    return f"\033[{row};{col}H"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_URL   = os.getenv("VECTRAFI_TARGET_URL", "http://localhost:8000")
POLL_SECS    = 2.0          # refresh interval
TAX_RATE     = 0.015        # 1.5 % protocol micro-tax — display reference only

STATS_PATH   = "/api/v1/analytics/stats"
TREASURY_PATH= "/api/v1/analytics/treasury"
RECENT_PATH  = "/api/v1/analytics/recent-transactions"

# ---------------------------------------------------------------------------
# Shared state — populated by polling coroutines, read by renderer
# ---------------------------------------------------------------------------

@dataclass
class StatsData:
    total_transactions:  int   = 0
    total_volume_usdc:   float = 0.0
    active_wallets:      int   = 0
    success_rate_pct:    float = 100.0
    failure_count:       int   = 0
    error:               str   = ""


@dataclass
class TreasuryData:
    accumulated_fees_usdc: float = 0.0
    total_volume_usdc:     float = 0.0
    error:                 str   = ""


@dataclass
class RecentTx:
    tx_id:       str   = ""
    sender_id:   str   = ""
    receiver_id: str   = ""
    gross_usdc:  float = 0.0
    tax_usdc:    float = 0.0
    net_usdc:    float = 0.0
    tx_type:     str   = ""


@dataclass
class RecentData:
    transactions: list[RecentTx] = field(default_factory=list)
    error:        str             = ""


@dataclass
class DashboardState:
    stats:        StatsData   = field(default_factory=StatsData)
    treasury:     TreasuryData = field(default_factory=TreasuryData)
    recent:       RecentData   = field(default_factory=RecentData)
    last_refresh: float        = 0.0
    tx_velocity:  float        = 0.0     # Tx/sec computed from successive stats polls
    _prev_tx_count: int        = 0
    _prev_ts:       float      = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Polling helpers
# ---------------------------------------------------------------------------

async def _get(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    resp = await client.get(f"{TARGET_URL}{path}", timeout=5.0)
    resp.raise_for_status()
    return resp.json()


async def poll_stats(client: httpx.AsyncClient, state: DashboardState) -> None:
    try:
        data = await _get(client, STATS_PATH)
        s = state.stats
        s.total_transactions = int(data.get("total_transactions_processed", data.get("total_transactions", 0)))
        s.total_volume_usdc  = float(data.get("total_volume_processed_usdc", data.get("total_volume_usdc", 0.0)))
        s.active_wallets     = int(data.get("active_wallets_count", data.get("active_wallets", 0)))
        s.success_rate_pct   = float(data.get("success_rate_pct", 100.0))
        s.failure_count      = int(data.get("failure_count", 0))
        s.error              = ""

        # Compute Tx/sec from successive readings
        now = time.monotonic()
        dt  = now - state._prev_ts
        if dt > 0 and state._prev_tx_count > 0:
            state.tx_velocity = (s.total_transactions - state._prev_tx_count) / dt
        state._prev_tx_count = s.total_transactions
        state._prev_ts       = now
    except httpx.HTTPStatusError as exc:
        state.stats.error = f"HTTP {exc.response.status_code}"
    except httpx.RequestError as exc:
        state.stats.error = f"unreachable ({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001
        state.stats.error = str(exc)[:50]


async def poll_treasury(client: httpx.AsyncClient, state: DashboardState) -> None:
    try:
        data = await _get(client, TREASURY_PATH)
        t = state.treasury
        t.accumulated_fees_usdc = float(data.get("accumulated_fees_usdc", 0.0))
        t.total_volume_usdc     = float(data.get("total_volume_processed_usdc", data.get("total_volume_usdc", 0.0)))
        t.error                 = ""
    except httpx.HTTPStatusError as exc:
        # Fall back: try the existing settlement analytics endpoint
        if exc.response.status_code == 404:
            try:
                data = await _get(client, "/api/v1/settlement/analytics")
                state.treasury.accumulated_fees_usdc = float(data.get("accumulated_fees_usdc", 0.0))
                state.treasury.total_volume_usdc     = float(data.get("total_volume_processed_usdc", 0.0))
                state.treasury.error = ""
                return
            except Exception:
                pass
        state.treasury.error = f"HTTP {exc.response.status_code}"
    except httpx.RequestError as exc:
        state.treasury.error = f"unreachable ({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001
        state.treasury.error = str(exc)[:50]


async def poll_recent(client: httpx.AsyncClient, state: DashboardState) -> None:
    try:
        data = await _get(client, RECENT_PATH)
        txs: list[RecentTx] = []
        raw = data if isinstance(data, list) else data.get("transactions", [])
        for item in raw[:5]:
            txs.append(RecentTx(
                tx_id      = str(item.get("tx_id", ""))[:8],
                sender_id  = str(item.get("sender_id", item.get("agent_id", "?"))),
                receiver_id= str(item.get("receiver_id", "?")),
                gross_usdc = float(item.get("gross_amount_usdc", item.get("gross_usdc", 0.0))),
                tax_usdc   = float(item.get("tax_amount_usdc",   item.get("tax_usdc",   0.0))),
                net_usdc   = float(item.get("net_amount_usdc",   item.get("net_usdc",   0.0))),
                tx_type    = str(item.get("tx_type", "")),
            ))
        state.recent.transactions = txs
        state.recent.error        = ""
    except httpx.HTTPStatusError as exc:
        state.recent.error = f"HTTP {exc.response.status_code}"
    except httpx.RequestError as exc:
        state.recent.error = f"unreachable ({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001
        state.recent.error = str(exc)[:50]


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_W = 80   # terminal width used for layout

def _hline(ch: str = "─", width: int = _W) -> str:
    return ch * width


def _box_top(title: str, width: int) -> str:
    inner = width - 2
    label = f" {title} "
    pad   = inner - len(label)
    return f"┌{label}{'─' * pad}┐"


def _box_bot(width: int) -> str:
    return f"└{'─' * (width - 2)}┘"


def _row(content: str, width: int) -> str:
    inner = width - 4
    return f"│  {content[:inner]:<{inner}}  │"


def _pad(s: str, width: int) -> str:
    return f"{s:<{width}}"


import re as _re
_ANSI_RE = _re.compile(r"\033\[[0-9;]*[mHJK]")


def _visible_len(s: str) -> int:
    """Return the printable width of a string, ignoring ANSI escape codes."""
    return len(_ANSI_RE.sub("", s))


def _status_dot(error: str) -> str:
    return f"{RED}●{RESET}" if error else f"{GREEN}●{RESET}"


def _render_frame(state: DashboardState) -> str:
    """Build the complete dashboard frame as a single string."""
    lines: list[str] = []
    W = _W

    # ── Header ──────────────────────────────────────────────────────────────
    lines.append(f"{BOLD}{CYAN}{'=' * W}{RESET}")
    title = "=== VECTRAFI PROTOCOL TELEMETRY GATEWAY ==="
    lines.append(f"{BOLD}{WHITE}{title:^{W}}{RESET}")
    lines.append(f"{BOLD}{CYAN}{'=' * W}{RESET}")
    lines.append("")

    # ── Treasury + Network Health (side by side) ─────────────────────────────
    LW, RW = 42, 36   # left width, right width (gap = W - LW - RW - 2)

    t  = state.treasury
    s  = state.stats

    fee_str   = f"{t.accumulated_fees_usdc:.8f} USDC"
    vol_str   = f"{t.total_volume_usdc:,.2f} USDC"
    vel_str   = f"{state.tx_velocity:.2f} Tx/sec"

    t_dot = _status_dot(t.error)
    s_dot = _status_dot(s.error)

    # Row 1 of widgets
    lines.append(
        f"┌─ {BOLD}TREASURY VAULT STATUS{RESET} {t_dot} {'─' * (LW - 27)}┐"
        f"  "
        f"┌─ {BOLD}NETWORK HEALTH{RESET} {s_dot} {'─' * (RW - 20)}┐"
    )

    def left_right(l: str, r: str) -> str:
        lpad = max(0, LW - 4 - _visible_len(l))
        rpad = max(0, RW - 4 - _visible_len(r))
        lf = f"│  {l}{' ' * lpad}  │"
        rf = f"│  {r}{' ' * rpad}  │"
        return f"{lf}  {rf}"

    lines.append(left_right(
        f"{BOLD}Accumulated Fees (1.5% micro-tax){RESET}",
        f"Tx Velocity"
    ))
    lines.append(left_right(
        f"{BOLD}{GREEN}{fee_str}{RESET}" if not t.error else f"{RED}{t.error}{RESET}",
        f"{BOLD}{YELLOW}{vel_str}{RESET}" if not s.error else f"{RED}{s.error}{RESET}"
    ))
    lines.append(left_right("", ""))
    lines.append(left_right(
        f"Total Volume:  {vol_str}" if not t.error else "",
        f"Total Txns:    {s.total_transactions}"
    ))
    lines.append(left_right(
        f"Tax Rate:      {TAX_RATE * 100:.1f}%",
        f"Active Agents: {s.active_wallets}"
    ))
    lines.append(left_right(
        "",
        f"Success Rate:  {s.success_rate_pct:.1f}%"
    ))
    lines.append(
        f"└{'─' * (LW - 2)}┘"
        f"  "
        f"└{'─' * (RW - 2)}┘"
    )
    lines.append("")

    # ── Rolling Ledger ────────────────────────────────────────────────────────
    lines.append(f"┌─ {BOLD}ROLLING LEDGER{RESET} — last 5 signed settlement actions {'─' * (W - 50)}┐")
    hdr = f"{'TX ID':<10}  {'SENDER':<20}  {'RECEIVER':<20}  {'GROSS':>12}  {'TAX':>12}  {'NET':>12}"
    hdr_pad = max(0, W - 4 - _visible_len(hdr))
    lines.append(f"│  {BOLD}{hdr}{RESET}{'':>{hdr_pad}}│")
    lines.append(f"│  {'─' * (W - 4)}  │" if False else f"│  {DIM}{'─' * (W - 4)}{RESET}  │")

    r = state.recent
    if r.error:
        err_content = f"{RED}Error: {r.error}{RESET}"
        err_pad = max(0, W - 4 - _visible_len(err_content))
        lines.append(f"│  {err_content}{' ' * err_pad}  │")
        for _ in range(4):
            lines.append(f"│  {' ' * (W - 4)}  │")
    elif not r.transactions:
        empty_msg = f"{DIM}No transactions yet — waiting for settlement activity...{RESET}"
        empty_pad = max(0, W - 4 - _visible_len(empty_msg))
        lines.append(f"│  {empty_msg}{' ' * empty_pad}  │")
        for _ in range(4):
            lines.append(f"│  {' ' * (W - 4)}  │")
    else:
        shown = r.transactions[:5]
        for tx in shown:
            sender   = tx.sender_id[:18]
            receiver = tx.receiver_id[:18]
            row_str  = (
                f"{CYAN}{tx.tx_id:<10}{RESET}  "
                f"{sender:<20}  "
                f"{receiver:<20}  "
                f"{GREEN}{tx.gross_usdc:>12.8f}{RESET}  "
                f"{RED}{tx.tax_usdc:>12.8f}{RESET}  "
                f"{YELLOW}{tx.net_usdc:>12.8f}{RESET}"
            )
            row_pad = max(0, W - 4 - _visible_len(row_str))
            lines.append(f"│  {row_str}{' ' * row_pad}  │")
        for _ in range(5 - len(shown)):
            lines.append(f"│  {'':>{W - 4}}  │")

    lines.append(f"└{'─' * (W - 2)}┘")
    lines.append("")

    # ── Status bar ────────────────────────────────────────────────────────────
    refresh_ago = time.monotonic() - state.last_refresh if state.last_refresh else 0.0
    now_str     = time.strftime("%H:%M:%S", time.gmtime())
    status_bar  = (
        f"{DIM}[TARGET: {TARGET_URL}]"
        f"  [UTC: {now_str}]"
        f"  [Refreshed {refresh_ago:.1f}s ago]"
        f"  [POLL: {POLL_SECS:.1f}s]"
        f"  [Ctrl+C to quit]{RESET}"
    )
    lines.append(status_bar)

    # Join — each line clears to end of line to erase leftover chars from prior frame
    return (HOME + "".join(line + CLREOL + "\n" for line in lines))


# ---------------------------------------------------------------------------
# Async main loop
# ---------------------------------------------------------------------------

async def run_dashboard(state: DashboardState) -> None:
    # Clear screen once at startup
    sys.stdout.write(CLEAR + HOME)
    sys.stdout.flush()

    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            await asyncio.gather(
                poll_stats(client, state),
                poll_treasury(client, state),
                poll_recent(client, state),
            )
            state.last_refresh = time.monotonic()

            frame = _render_frame(state)
            sys.stdout.write(frame)
            sys.stdout.flush()

            await asyncio.sleep(POLL_SECS)


# ---------------------------------------------------------------------------
# Dry-run validation — no network calls
# ---------------------------------------------------------------------------

def dry_run() -> None:
    """Validate that the dashboard initialises without errors and print layout preview."""
    print(f"\n{BOLD}{BLUE}=== VectraFi Dashboard — DRY RUN VALIDATION ==={RESET}\n")

    # Instantiate all dataclasses to confirm no errors
    state = DashboardState()
    state.stats.total_transactions  = 42
    state.stats.total_volume_usdc   = 1_234.56789
    state.stats.active_wallets      = 3
    state.stats.success_rate_pct    = 98.5
    state.treasury.accumulated_fees_usdc = 18.54321987
    state.treasury.total_volume_usdc     = 1_234.56789
    state.tx_velocity                    = 0.87
    state.recent.transactions = [
        RecentTx("abcd1234", "alpha-dry-run", "beta-dry-run",  40.0, 0.6,  39.4,  "bounty_claim"),
        RecentTx("ef012345", "gamma-dry-run", "alpha-dry-run",  0.12, 0.0018, 0.1182, "compute_lease"),
    ]
    state.last_refresh = time.monotonic()

    # Render a sample frame to confirm rendering logic is error-free
    frame = _render_frame(state)

    print(f"{GREEN}{BOLD}[OK] DashboardState initialised successfully{RESET}")
    print(f"{GREEN}{BOLD}[OK] _render_frame() produced {len(frame)} bytes — no rendering errors{RESET}")
    print(f"{GREEN}{BOLD}[OK] All imports loaded (asyncio, httpx, sys, os, time, dataclasses){RESET}")
    print(f"\n{DIM}Configuration:{RESET}")
    print(f"  Target URL  : {CYAN}{TARGET_URL}{RESET}")
    print(f"  Stats path  : {STATS_PATH}")
    print(f"  Treasury    : {TREASURY_PATH}")
    print(f"  Recent Txns : {RECENT_PATH}")
    print(f"  Poll period : {POLL_SECS}s")
    print(f"  Tax display : {TAX_RATE * 100:.1f}%")
    print(f"\n{BOLD}Sample frame preview (first 20 lines):{RESET}\n")

    # Strip ANSI codes for clean preview in non-interactive terminals
    clean_frame = _ANSI_RE.sub("", frame)
    for i, line in enumerate(clean_frame.splitlines()[:20]):
        print(f"  {line}")

    print(f"\n{GREEN}{BOLD}Dry run complete — dashboard boots cleanly. No network calls made.{RESET}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="VectraFi Protocol Telemetry Gateway — real-time terminal dashboard",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate initialisation and print a layout preview; make no network calls",
    )
    p.add_argument(
        "--poll",
        type=float,
        default=POLL_SECS,
        metavar="SECONDS",
        help=f"Poll interval in seconds (default: {POLL_SECS})",
    )
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args   = parser.parse_args()

    if args.poll != POLL_SECS:
        POLL_SECS = args.poll  # allow override at runtime

    if args.dry_run:
        dry_run()
        sys.exit(0)

    state = DashboardState()
    try:
        asyncio.run(run_dashboard(state))
    except KeyboardInterrupt:
        # Restore cursor and leave a clean exit line
        sys.stdout.write(f"\033[?25h\n{RESET}{BOLD}Dashboard stopped.{RESET}\n")
        sys.stdout.flush()
        sys.exit(0)
