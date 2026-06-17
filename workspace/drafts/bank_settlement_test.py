"""
Exhaustive test suite for the micro-bank settlement layer.
Each test uses a fresh, isolated SQLite database via tmp_path to prevent
cross-test contamination.
"""

import pytest
from pathlib import Path
from bank_settlement import (
    claim_bounty,
    init_db,
    get_wallet,
    list_wallets,
    execute_agent_transaction,
    InsufficientFunds,
    get_balance,
    get_connection,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fresh_db(tmp_path: Path) -> Path:
    db = tmp_path / "test_bank.db"
    init_db(db)
    return db


# ---------------------------------------------------------------------------
# 1. Seed state verification
# ---------------------------------------------------------------------------

class TestSeedState:
    def test_treasury_seeded_at_zero(self, tmp_path):
        db = fresh_db(tmp_path)
        w = get_wallet("treasury", db)
        assert w["balance"] == 0

    def test_agent_zero_seeded_at_10000(self, tmp_path):
        db = fresh_db(tmp_path)
        w = get_wallet("agent-zero", db)
        assert w["balance"] == 10_000

    def test_agent_one_seeded_at_10000(self, tmp_path):
        db = fresh_db(tmp_path)
        w = get_wallet("agent-one", db)
        assert w["balance"] == 10_000

    def test_all_three_wallets_exist(self, tmp_path):
        db = fresh_db(tmp_path)
        wallets = list_wallets(db)
        ids = {w["agent_id"] for w in wallets}
        assert ids == {"treasury", "agent-zero", "agent-one"}

    def test_identifier_is_not_key_material(self, tmp_path):
        """Confirms identifier column holds plain labels, not hex/base58 keys."""
        db = fresh_db(tmp_path)
        for w in list_wallets(db):
            assert len(w["identifier"]) < 64, (
                f"identifier for {w['agent_id']} looks like key material"
            )

    def test_init_is_idempotent(self, tmp_path):
        """Re-running init_db must not reset existing balances."""
        db = fresh_db(tmp_path)
        execute_agent_transaction("agent-zero", "agent-one", 500, "test", db_path=db)
        init_db(db)   # second call
        conn = get_connection(db)
        bal = get_balance("agent-zero", conn)
        conn.close()
        assert bal == 9_500   # not reset to 10_000


# ---------------------------------------------------------------------------
# 2. Micro-tax arithmetic
# ---------------------------------------------------------------------------

class TestMicroTax:
    def test_tax_rate_is_15_bps(self, tmp_path):
        db = fresh_db(tmp_path)
        tx = execute_agent_transaction("agent-zero", "agent-one", 1000, "test", db_path=db)
        assert tx["tax_amount"] == 15          # 1000 * 15 // 1000

    def test_net_amount_equals_gross_minus_tax(self, tmp_path):
        db = fresh_db(tmp_path)
        tx = execute_agent_transaction("agent-zero", "agent-one", 2000, "test", db_path=db)
        assert tx["net_amount"] == tx["gross_amount"] - tx["tax_amount"]

    def test_treasury_receives_exact_tax(self, tmp_path):
        db = fresh_db(tmp_path)
        execute_agent_transaction("agent-zero", "agent-one", 1000, "test", db_path=db)
        conn = get_connection(db)
        treasury_bal = get_balance("treasury", conn)
        conn.close()
        assert treasury_bal == 15

    def test_receiver_gets_net_not_gross(self, tmp_path):
        db = fresh_db(tmp_path)
        execute_agent_transaction("agent-zero", "agent-one", 1000, "test", db_path=db)
        conn = get_connection(db)
        bal = get_balance("agent-one", conn)
        conn.close()
        assert bal == 10_000 + 985   # seeded 10_000 + net 985

    def test_sender_debited_full_gross(self, tmp_path):
        db = fresh_db(tmp_path)
        execute_agent_transaction("agent-zero", "agent-one", 1000, "test", db_path=db)
        conn = get_connection(db)
        bal = get_balance("agent-zero", conn)
        conn.close()
        assert bal == 9_000   # 10_000 - 1_000

    def test_conservation_of_units(self, tmp_path):
        """Total units across all wallets must remain constant."""
        db = fresh_db(tmp_path)
        total_before = sum(w["balance"] for w in list_wallets(db))
        execute_agent_transaction("agent-zero", "agent-one", 3000, "test", db_path=db)
        total_after = sum(w["balance"] for w in list_wallets(db))
        assert total_before == total_after


# ---------------------------------------------------------------------------
# 3. Insufficient funds / rollback
# ---------------------------------------------------------------------------

class TestRollback:
    def test_overdraft_raises(self, tmp_path):
        db = fresh_db(tmp_path)
        with pytest.raises(InsufficientFunds):
            execute_agent_transaction("agent-zero", "agent-one", 99_999, "test", db_path=db)

    def test_rollback_leaves_balances_unchanged(self, tmp_path):
        db = fresh_db(tmp_path)
        conn = get_connection(db)
        before_zero  = get_balance("agent-zero", conn)
        before_one   = get_balance("agent-one", conn)
        before_treas = get_balance("treasury", conn)
        conn.close()

        with pytest.raises(InsufficientFunds):
            execute_agent_transaction("agent-zero", "agent-one", 50_000, "test", db_path=db)

        conn = get_connection(db)
        assert get_balance("agent-zero",  conn) == before_zero
        assert get_balance("agent-one",   conn) == before_one
        assert get_balance("treasury",    conn) == before_treas
        conn.close()

    def test_zero_amount_raises(self, tmp_path):
        db = fresh_db(tmp_path)
        with pytest.raises(ValueError):
            execute_agent_transaction("agent-zero", "agent-one", 0, "test", db_path=db)

    def test_same_sender_receiver_raises(self, tmp_path):
        db = fresh_db(tmp_path)
        with pytest.raises(ValueError):
            execute_agent_transaction("agent-zero", "agent-zero", 100, "test", db_path=db)


# ---------------------------------------------------------------------------
# 4. Multi-hop: Agent Zero -> Agent One -> Treasury
# ---------------------------------------------------------------------------

class TestMultiHop:
    def test_two_hop_chain(self, tmp_path):
        """agent-zero pays agent-one; agent-one pays treasury directly."""
        db = fresh_db(tmp_path)

        tx1 = execute_agent_transaction("agent-zero", "agent-one", 2000, "hop1", db_path=db)
        # agent-one now has 10_000 + 1_970 (net after 1.5% tax on 2000)
        tx2 = execute_agent_transaction("agent-one", "treasury", 1000, "hop2", db_path=db)

        conn = get_connection(db)
        bal_zero  = get_balance("agent-zero", conn)
        bal_one   = get_balance("agent-one",  conn)
        bal_treas = get_balance("treasury",   conn)
        conn.close()

        # agent-zero: 10_000 - 2_000 = 8_000
        assert bal_zero == 8_000
        # agent-one: received 1_970 from hop1, paid 1_000 in hop2 → 10_970
        assert bal_one == 10_000 + tx1["net_amount"] - 1_000
        # treasury: tax from hop1 (30) + full gross from hop2 (1_000).
        # When receiver IS treasury it collects both the routed tax (15)
        # AND the net credit (985), totalling the full gross amount.
        expected_treasury = tx1["tax_amount"] + tx2["gross_amount"]
        assert bal_treas == expected_treasury

    def test_cumulative_tax_across_three_transactions(self, tmp_path):
        db = fresh_db(tmp_path)
        total_tax = 0
        for amount in [500, 1000, 1500]:
            tx = execute_agent_transaction("agent-zero", "agent-one", amount, "multi", db_path=db)
            total_tax += tx["tax_amount"]
        conn = get_connection(db)
        treasury_bal = get_balance("treasury", conn)
        conn.close()
        assert treasury_bal == total_tax


# ---------------------------------------------------------------------------
# 5. claim_bounty integration
# ---------------------------------------------------------------------------

class TestClaimBounty:
    def test_bounty_split_reduces_claimant_balance(self, tmp_path):
        db = fresh_db(tmp_path)
        result = claim_bounty("agent-zero", 600, "agent-one", db_path=db)
        assert result["post_balances"]["agent-zero"] < 10_000

    def test_counterpart_receives_net_share(self, tmp_path):
        db = fresh_db(tmp_path)
        result = claim_bounty("agent-zero", 600, "agent-one", db_path=db)
        counterpart_bal = result["post_balances"]["agent-one"]
        # agent-one started at 10_000 and received counterpart net share
        assert counterpart_bal > 10_000

    def test_treasury_collects_tax_on_split(self, tmp_path):
        db = fresh_db(tmp_path)
        result = claim_bounty("agent-zero", 600, "agent-one", db_path=db)
        assert result["total_tax_collected"] > 0
        assert result["post_balances"]["treasury"] == result["total_tax_collected"]

    def test_unit_conservation_across_bounty(self, tmp_path):
        db = fresh_db(tmp_path)
        total_before = sum(w["balance"] for w in list_wallets(db))
        claim_bounty("agent-zero", 600, "agent-one", db_path=db)
        total_after = sum(w["balance"] for w in list_wallets(db))
        assert total_before == total_after

    def test_claim_bounty_same_agent_raises(self, tmp_path):
        db = fresh_db(tmp_path)
        with pytest.raises(ValueError, match="differ"):
            claim_bounty("agent-zero", 100, "agent-zero", db_path=db)

    def test_claim_bounty_zero_amount_raises(self, tmp_path):
        db = fresh_db(tmp_path)
        with pytest.raises(ValueError, match="positive"):
            claim_bounty("agent-zero", 0, "agent-one", db_path=db)
