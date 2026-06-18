#!/usr/bin/env python3
"""
propose_optimization.py — VectraFi Phase 2 Routing Optimization Pipeline

Applies a documented set of performance rules to routing configuration
constants, validates the change against the full test suite, then either
commits the result to a timestamped feature branch or reverts cleanly.

Safety invariants:
  - Always operates on a fresh branch, never touches main/HEAD directly.
  - On ANY test failure the workspace is fully restored before exit.
  - Remote push is opt-in (--push flag or ALLOW_AUTO_PUSH=1 env var).
    An autonomous script should never push to a shared remote without
    explicit authorisation from the operator.

Environment variables:
  ALLOW_AUTO_PUSH   Set to "1" to enable push without --push flag (CI use).
  PYTEST_EXTRA_ARGS Space-separated extra args forwarded to pytest.

Usage:
  python scripts/propose_optimization.py
  python scripts/propose_optimization.py --push
  python scripts/propose_optimization.py --dry-run   (diff only, no git ops)
  python scripts/propose_optimization.py --branch-prefix phase2/mainnet
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("vectrafi.opt")

# ---------------------------------------------------------------------------
# Repo paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ARBITRAGE_PY = _REPO_ROOT / "core-exchange" / "src" / "routes" / "arbitrage.py"
_CONFIG_PY    = _REPO_ROOT / "core-exchange" / "src" / "config.py"
_TESTS_DIR    = _REPO_ROOT / "core-exchange"


# ---------------------------------------------------------------------------
# Optimization rule definitions
# ---------------------------------------------------------------------------

class OptRule(NamedTuple):
    file: Path
    param: str
    pattern: str          # regex that captures the full assignment line
    replacement: str      # replacement string (may use backreference \1 for prefix)
    old_value: str        # human-readable current value
    new_value: str        # human-readable target value
    rationale: str


RULES: list[OptRule] = [
    OptRule(
        file=_ARBITRAGE_PY,
        param="_CANDIDATE_CAP",
        pattern=r"(_CANDIDATE_CAP\s*=\s*)\d+",
        replacement=r"\g<1>15",
        old_value="10",
        new_value="15",
        rationale=(
            "Wider relay candidate pool (15 vs 10) gives the rebalancer more "
            "donor options on mainnet where agent count is growing."
        ),
    ),
    OptRule(
        file=_ARBITRAGE_PY,
        param="_GAS_COST_PER_HOP",
        pattern=r'(_GAS_COST_PER_HOP\s*=\s*Decimal\(")[^"]+("\))',
        replacement=r'\g<1>0.03\2',
        old_value="0.05",
        new_value="0.03",
        rationale=(
            "Base L2 average gas per ERC-20 transfer is ~$0.02-0.04. "
            "Tightening the static friction constant from 0.05->0.03 USDC "
            "improves route viability scoring without exposing the protocol "
            "to gas risk (actual gas is paid by the calling wallet)."
        ),
    ),
    OptRule(
        file=_CONFIG_PY,
        param="HTTP_TIMEOUT_SECONDS",
        pattern=r"(HTTP_TIMEOUT_SECONDS\s*=\s*)\d+\.?\d*",
        replacement=r"\g<1>8.0",
        old_value="5.0",
        new_value="8.0",
        rationale=(
            "Mainnet Coinbase price-feed latency is occasionally 5-7 s under "
            "load. Increasing the timeout to 8 s eliminates spurious fallback "
            "to FALLBACK_PRICES during normal operation."
        ),
    ),
    OptRule(
        file=_CONFIG_PY,
        param="PRICE_CACHE_TTL_SECONDS",
        pattern=r"(PRICE_CACHE_TTL_SECONDS\s*=\s*)\d+\.?\d*",
        replacement=r"\g<1>25.0",
        old_value="30.0",
        new_value="25.0",
        rationale=(
            "Fresher price data (25 s TTL vs 30 s) tightens arbitrage "
            "route quality on mainnet where ETH/HBAR spreads move quickly."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(_REPO_ROOT), *args]
    log.debug("git %s", " ".join(args))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _current_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _has_uncommitted_changes() -> bool:
    result = _git("status", "--porcelain", check=False)
    return bool(result.stdout.strip())


def _apply_rule(rule: OptRule) -> bool:
    """Apply one regex substitution to the target file. Returns True if file changed."""
    source = rule.file.read_text(encoding="utf-8")
    patched, count = re.subn(rule.pattern, rule.replacement, source)
    if count == 0:
        log.warning(
            "[skip] %s — pattern not found (file may have already been patched or diverged)",
            rule.param,
        )
        return False
    if patched == source:
        log.info("[skip] %s — value already at target (%s)", rule.param, rule.new_value)
        return False
    rule.file.write_text(patched, encoding="utf-8")
    log.info(
        "[patch] %-28s  %s -> %s  (%s)",
        rule.param, rule.old_value, rule.new_value,
        rule.file.relative_to(_REPO_ROOT),
    )
    return True


def _revert_workspace(original_branch: str, pop_stash: bool = False) -> None:
    log.warning("Reverting workspace to clean state...")
    _git("restore", ".", check=False)
    _git("checkout", original_branch, check=False)
    if pop_stash:
        r = _git("stash", "pop", check=False)
        if r.returncode == 0:
            log.info("Pre-existing stash restored.")
        else:
            log.warning("Stash pop encountered a conflict — resolve manually with: git stash pop")
    log.info("Workspace restored. Branch unchanged: %s", original_branch)


def _run_tests() -> tuple[bool, str]:
    """Run pytest against the full test suite. Returns (passed, summary_line)."""
    extra = os.getenv("PYTEST_EXTRA_ARGS", "").split()
    cmd = [
        sys.executable, "-m", "pytest",
        "--tb=short",
        "-q",
        *extra,
    ]
    log.info("Running test suite: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        cwd=str(_TESTS_DIR),
        capture_output=False,   # stream to console in real-time
        text=True,
    )
    passed = result.returncode == 0
    summary = "PASSED" if passed else f"FAILED (exit {result.returncode})"
    return passed, summary


def _print_diff_preview(applied: list[OptRule]) -> None:
    print("\nOPTIMIZATION RULES TO APPLY")
    print("=" * 72)
    for rule in applied:
        print(f"\n  [{rule.param}]")
        print(f"    File     : {rule.file.relative_to(_REPO_ROOT)}")
        print(f"    Change   : {rule.old_value} -> {rule.new_value}")
        print(f"    Rationale: {rule.rationale}")
    print()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="VectraFi Phase 2 routing optimization pipeline"
    )
    parser.add_argument(
        "--push", action="store_true",
        help="Push the feature branch to origin after a green test run",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the optimization plan without making any git or file changes",
    )
    parser.add_argument(
        "--branch-prefix", default="phase2/auto-opt-patch",
        help="Branch name prefix (default: phase2/auto-opt-patch)",
    )
    args = parser.parse_args()

    allow_push = args.push or os.getenv("ALLOW_AUTO_PUSH", "0") == "1"

    # ------------------------------------------------------------------
    # 0. Guard: must be run from within the git repo
    # ------------------------------------------------------------------
    if not (_REPO_ROOT / ".git").exists():
        log.error("Not inside a git repository (expected .git at %s)", _REPO_ROOT)
        sys.exit(1)

    original_branch = _current_branch()
    log.info("Current branch: %s", original_branch)

    # ------------------------------------------------------------------
    # 1. Print optimization plan
    # ------------------------------------------------------------------
    _print_diff_preview(RULES)

    if args.dry_run:
        log.info("DRY RUN — no files modified, no git operations executed.")
        sys.exit(0)

    # ------------------------------------------------------------------
    # Workspace guard (post dry-run, pre any git op)
    # Stash pre-existing changes so the script can branch cleanly and
    # restore is scoped only to the script's own patches on failure.
    # ------------------------------------------------------------------
    stashed = False
    if _has_uncommitted_changes():
        log.info("Pre-existing uncommitted changes detected — stashing before pipeline.")
        stash_result = _git("stash", "push", "-u", "-m", "propose_optimization: auto-stash", check=False)
        if stash_result.returncode != 0:
            log.error(
                "git stash failed — commit or manually stash your changes first.\n%s",
                stash_result.stderr.strip(),
            )
            sys.exit(1)
        stashed = True
        log.info("Stashed. Will restore after pipeline completes.")

    # ------------------------------------------------------------------
    # 2. Create timestamped feature branch
    # ------------------------------------------------------------------
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    branch = f"{args.branch_prefix}-{ts}"
    log.info("Creating branch: %s", branch)
    _git("checkout", "-b", branch)

    # ------------------------------------------------------------------
    # 3. Apply optimization rules
    # ------------------------------------------------------------------
    applied_count = 0
    for rule in RULES:
        if _apply_rule(rule):
            applied_count += 1

    if applied_count == 0:
        log.warning("No rules produced file changes — all parameters already at target values.")
        _git("checkout", original_branch)
        _git("branch", "-d", branch)
        sys.exit(0)

    log.info("%d rule(s) applied successfully.", applied_count)

    # ------------------------------------------------------------------
    # 4. Run full test suite
    # ------------------------------------------------------------------
    passed, summary = _run_tests()
    log.info("Test suite result: %s", summary)

    if not passed:
        log.error("Tests FAILED — reverting all changes to protect the workspace.")
        _revert_workspace(original_branch, pop_stash=stashed)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 5. Commit on green baseline
    # ------------------------------------------------------------------
    log.info("Tests passed — committing optimization patch.")
    _git("add",
        str(_ARBITRAGE_PY.relative_to(_REPO_ROOT)),
        str(_CONFIG_PY.relative_to(_REPO_ROOT)),
    )

    rules_summary = "; ".join(
        f"{r.param} {r.old_value}->{r.new_value}" for r in RULES
    )
    commit_msg = (
        f"perf: phase2 routing optimisation — {applied_count} rule(s)\n\n"
        f"{rules_summary}\n\n"
        f"Generated by scripts/propose_optimization.py on {ts}.\n"
        f"All {sum(1 for _ in RULES)} performance rules applied and verified "
        f"against the full test suite before commit.\n\n"
        f"Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
    )
    _git("commit", "-m", commit_msg)
    log.info("Committed on branch %s", branch)

    # ------------------------------------------------------------------
    # 6. Push (opt-in only)
    # ------------------------------------------------------------------
    if allow_push:
        log.info("Pushing branch %s to origin...", branch)
        result = _git("push", "--set-upstream", "origin", branch, check=False)
        if result.returncode == 0:
            log.info("Branch pushed. Open a PR at:")
            log.info("  https://github.com/SgtClickClack/VectraFi/compare/%s", branch)
        else:
            log.error("Push failed:\n%s", result.stderr.strip())
            sys.exit(1)
    else:
        print("\n" + "=" * 72)
        print("BRANCH READY — push skipped (use --push or ALLOW_AUTO_PUSH=1 to publish)")
        print(f"  Branch  : {branch}")
        print(f"  To push : git push --set-upstream origin {branch}")
        print("=" * 72)

    # Pop the stash back onto the original branch (don't leave it dangling).
    if stashed:
        _git("checkout", original_branch, check=False)
        r = _git("stash", "pop", check=False)
        if r.returncode == 0:
            log.info("Pre-existing stash restored to %s.", original_branch)
        else:
            log.warning(
                "Stash pop encountered a conflict — resolve manually: git stash pop"
            )
        # Return to the feature branch so the operator can inspect/push it.
        _git("checkout", branch, check=False)

    log.info("Pipeline complete. Branch: %s", branch)


if __name__ == "__main__":
    main()
