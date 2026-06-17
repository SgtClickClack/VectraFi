"""
Integration tests for:
  1. faba_server.py MCP tool schemas (all 4 tools, incl. new get_agent_balance)
  2. swarm_orchestrator: 3-step simulation correctness and tax accounting
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT  = Path(__file__).resolve().parent.parent
_WORKSPACE  = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)   # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)                  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fresh_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("swarm") / "test_swarm.db"
    ledger = _load("bank_ledger", _WORKSPACE / "bank_ledger.py")
    ledger.init_db(db)
    return db


@pytest.fixture(scope="module")
def orchestrator():
    return _load("swarm_orchestrator", _WORKSPACE / "swarm_orchestrator.py")


# ---------------------------------------------------------------------------
# 1. MCP tool schema validation (subprocess — requires venv with mcp installed)
# ---------------------------------------------------------------------------

_VENV_PYTHON  = _REPO_ROOT / ".venv" / "Scripts" / "python.exe"
_SERVER_PATH  = _REPO_ROOT / "mcp" / "faba_server.py"
_PROBE_SCRIPT = _WORKSPACE / "_mcp_probe.py"

# Written once at import time; cleaned up by the fixture after use.
_PROBE_SCRIPT.write_text(
    f"""
import json, sys, importlib.util
spec = importlib.util.spec_from_file_location("faba_server", r"{_SERVER_PATH}")
mod  = importlib.util.module_from_spec(spec)
sys.modules["faba_server"] = mod
spec.loader.exec_module(mod)
tools = mod.mcp._tool_manager.list_tools()
out = [{{
    "name": t.name,
    "description": bool(t.description),
    "params": list((t.parameters or {{}}).get("properties", {{}}).keys()),
}} for t in tools]
print(json.dumps(out))
""",
    encoding="utf-8",
)


@pytest.fixture(scope="module")
def mcp_tools():
    import subprocess
    result = subprocess.run(
        [str(_VENV_PYTHON), str(_PROBE_SCRIPT)],
        capture_output=True, text=True, timeout=15,
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, f"MCP schema probe failed:\n{result.stderr}"
    return {t["name"]: t for t in json.loads(result.stdout)}


class TestMCPSchema:
    """Verify faba_server registers expected tools with valid inputSchema."""

    def test_inspect_faba_bounties_registered(self, mcp_tools):
        assert "inspect_faba_bounties" in mcp_tools

    def test_get_protocol_state_registered(self, mcp_tools):
        assert "get_protocol_state" in mcp_tools

    def test_generate_eip191_template_registered(self, mcp_tools):
        assert "generate_eip191_template" in mcp_tools

    def test_get_agent_balance_registered(self, mcp_tools):
        assert "get_agent_balance" in mcp_tools

    def test_get_agent_balance_has_agent_id_param(self, mcp_tools):
        assert "agent_id" in mcp_tools["get_agent_balance"]["params"]

    def test_no_mutating_transfer_tool_exposed(self, mcp_tools):
        """execute_settlement_transfer must NOT be on the public MCP server."""
        assert "execute_settlement_transfer" not in mcp_tools

    def test_no_claim_bounty_tool_exposed(self, mcp_tools):
        """claim_and_split_bounty must NOT be on the public MCP server."""
        assert "claim_and_split_bounty" not in mcp_tools

    def test_all_tools_have_descriptions(self, mcp_tools):
        for name, tool in mcp_tools.items():
            assert tool["description"], f"Tool '{name}' missing description"


# ---------------------------------------------------------------------------
# 2. Swarm orchestrator — Step 1: bounty inspection
# ---------------------------------------------------------------------------

class TestStep1:
    def test_returns_highest_yield_bounty(self, orchestrator, fresh_db):
        result = orchestrator.step1_inspect_bounties("agent-zero", db_path=fresh_db)
        # bounty-002 has yield 600 — should be selected
        assert result["selected"]["yield_units"] == 600

    def test_agent_balance_is_positive(self, orchestrator, fresh_db):
        result = orchestrator.step1_inspect_bounties("agent-zero", db_path=fresh_db)
        assert result["balance"] >= 0


# ---------------------------------------------------------------------------
# 3. Swarm orchestrator — Step 2: pool arrangement
# ---------------------------------------------------------------------------

class TestStep2:
    def test_weights_sum_to_one(self, orchestrator):
        result = orchestrator.step2_pool_arrangement(1000, 1000, 500)
        total = result["pool"].weights["agent-zero"] + result["pool"].weights["agent-one"]
        assert abs(total - 1.0) < 1e-9

    def test_shares_sum_to_bounty_yield(self, orchestrator):
        result = orchestrator.step2_pool_arrangement(2000, 1000, 300)
        assert result["zero_share"] + result["one_share"] == 300

    def test_higher_risk_multiplier_gets_larger_share(self, orchestrator):
        result = orchestrator.step2_pool_arrangement(1000, 1000, 1000)
        # agent-zero has risk_multiplier 1.2 vs agent-one 0.8
        assert result["zero_share"] > result["one_share"]


# ---------------------------------------------------------------------------
# 4. Swarm orchestrator — Step 3: settlement and tax accounting
# ---------------------------------------------------------------------------

class TestStep3:
    def test_tax_routed_to_treasury(self, orchestrator, fresh_db):
        ledger = _load("bank_ledger", _WORKSPACE / "bank_ledger.py")
        conn = ledger.get_connection(fresh_db)
        before = ledger.get_balance("treasury", conn)
        conn.close()

        result = orchestrator.step3_settle.__wrapped__ if hasattr(orchestrator.step3_settle, "__wrapped__") else None
        # Use claim_bounty directly on isolated DB to test tax routing
        settlement = _load("bank_settlement", _WORKSPACE / "validated" / "bank_settlement.py")
        result = settlement.claim_bounty("agent-zero", 600, "agent-one", db_path=fresh_db)

        conn = ledger.get_connection(fresh_db)
        after = ledger.get_balance("treasury", conn)
        conn.close()

        assert after - before == result["total_tax_collected"]
        assert result["total_tax_collected"] > 0

    def test_unit_conservation_across_settlement(self, orchestrator, fresh_db):
        ledger = _load("bank_ledger", _WORKSPACE / "bank_ledger.py")
        before_total = sum(w["balance"] for w in ledger.list_wallets(fresh_db))

        settlement = _load("bank_settlement", _WORKSPACE / "validated" / "bank_settlement.py")
        settlement.claim_bounty("agent-one", 200, "agent-zero", db_path=fresh_db)

        after_total = sum(w["balance"] for w in ledger.list_wallets(fresh_db))
        assert before_total == after_total

    def test_insufficient_funds_does_not_corrupt_db(self, orchestrator, fresh_db):
        ledger = _load("bank_ledger", _WORKSPACE / "bank_ledger.py")
        before = {w["agent_id"]: w["balance"] for w in ledger.list_wallets(fresh_db)}

        with pytest.raises(Exception):
            settlement = _load("bank_settlement", _WORKSPACE / "validated" / "bank_settlement.py")
            settlement.claim_bounty("agent-zero", 99_999, "agent-one", db_path=fresh_db)

        after = {w["agent_id"]: w["balance"] for w in ledger.list_wallets(fresh_db)}
        assert before == after
