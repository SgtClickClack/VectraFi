"""
Treasury fee collection tests — issue #3.

Covers:
- 0.25% protocol fee calculation precision
- 80/20 fee split between creator and bounty pool
- Edge cases: zero-value, minimum precision, large deposits
- Treasury state accumulation across multiple deposits
- Balance integrity after fee deduction
"""
import pytest
from config import (
    FEE_SPLIT_BOUNTY_RATE,
    FEE_SPLIT_CREATOR_RATE,
    HOLDING_ADDRESS_BOUNTY,
    HOLDING_ADDRESS_USER,
    PROTOCOL_FEE_RATE,
)


# ---------------------------------------------------------------------------
# Fee calculation precision
# ---------------------------------------------------------------------------


class TestFeeCalculation:
    """Verify exact fee arithmetic at floating-point precision boundaries."""

    def test_protocol_fee_exact_0_25_percent(self):
        amount = 1000.0
        expected_fee = round(amount * PROTOCOL_FEE_RATE, 8)
        assert expected_fee == 2.5

    def test_creator_fee_is_80_percent_of_protocol_fee(self):
        amount = 1000.0
        protocol_fee = round(amount * PROTOCOL_FEE_RATE, 8)
        creator_fee = round(protocol_fee * FEE_SPLIT_CREATOR_RATE, 8)
        assert creator_fee == 2.0

    def test_bounty_fee_is_20_percent_of_protocol_fee(self):
        amount = 1000.0
        protocol_fee = round(amount * PROTOCOL_FEE_RATE, 8)
        bounty_fee = round(protocol_fee * FEE_SPLIT_BOUNTY_RATE, 8)
        assert bounty_fee == 0.5

    def test_fee_split_sums_to_protocol_fee(self):
        amount = 1000.0
        protocol_fee = round(amount * PROTOCOL_FEE_RATE, 8)
        creator_fee = round(protocol_fee * FEE_SPLIT_CREATOR_RATE, 8)
        bounty_fee = round(protocol_fee * FEE_SPLIT_BOUNTY_RATE, 8)
        assert round(creator_fee + bounty_fee, 8) == protocol_fee

    def test_net_deposit_equals_amount_minus_fee(self):
        amount = 1000.0
        protocol_fee = round(amount * PROTOCOL_FEE_RATE, 8)
        net = round(amount - protocol_fee, 8)
        assert net == 997.5

    def test_small_amount_fee_precision(self):
        amount = 0.01
        protocol_fee = round(amount * PROTOCOL_FEE_RATE, 8)
        creator_fee = round(protocol_fee * FEE_SPLIT_CREATOR_RATE, 8)
        bounty_fee = round(protocol_fee * FEE_SPLIT_BOUNTY_RATE, 8)
        assert protocol_fee == pytest.approx(0.000025, abs=1e-8)
        assert round(creator_fee + bounty_fee, 8) == protocol_fee

    def test_large_amount_fee_precision(self):
        amount = 1_000_000.0
        protocol_fee = round(amount * PROTOCOL_FEE_RATE, 8)
        assert protocol_fee == 2500.0
        creator_fee = round(protocol_fee * FEE_SPLIT_CREATOR_RATE, 8)
        bounty_fee = round(protocol_fee * FEE_SPLIT_BOUNTY_RATE, 8)
        assert creator_fee == 2000.0
        assert bounty_fee == 500.0

    def test_odd_amount_fee_precision(self):
        amount = 123.456
        protocol_fee = round(amount * PROTOCOL_FEE_RATE, 8)
        creator_fee = round(protocol_fee * FEE_SPLIT_CREATOR_RATE, 8)
        bounty_fee = round(protocol_fee * FEE_SPLIT_BOUNTY_RATE, 8)
        assert round(creator_fee + bounty_fee, 8) == protocol_fee
        assert round(amount - protocol_fee, 8) + protocol_fee == amount


# ---------------------------------------------------------------------------
# Integration: deposit endpoint fee routing
# ---------------------------------------------------------------------------


class TestDepositFeeRouting:
    """End-to-end fee routing through the /api/v1/bank/deposit endpoint."""

    def test_deposit_applies_correct_fee(self, client, registered_wallet, sign_body):
        amount = 1000.0
        body = {
            "agent_id": registered_wallet["agent_id"],
            "wallet_address": registered_wallet["wallet_address"],
            "amount_usdc": amount,
        }
        sig = sign_body(registered_wallet["private_key"], body)
        r = client.post("/api/v1/bank/deposit", json=body, headers={"X-VectraFi-Signature": sig})
        assert r.status_code == 200
        data = r.json()

        assert data["amount_deposited"] == amount
        assert data["protocol_fee_usdc"] == pytest.approx(2.5, abs=1e-6)
        assert data["creator_fee_usdc"] == pytest.approx(2.0, abs=1e-6)
        assert data["bounty_pool_fee_usdc"] == pytest.approx(0.5, abs=1e-6)
        assert data["net_deposited_usdc"] == pytest.approx(997.5, abs=1e-6)

    def test_deposit_balance_decreased_by_gross(self, client, registered_wallet, sign_body):
        body = {
            "agent_id": registered_wallet["agent_id"],
            "wallet_address": registered_wallet["wallet_address"],
            "amount_usdc": 500.0,
        }
        sig = sign_body(registered_wallet["private_key"], body)
        r = client.post("/api/v1/bank/deposit", json=body, headers={"X-VectraFi-Signature": sig})
        assert r.status_code == 200
        data = r.json()
        # balance should be 100000 - 500 = 99500
        assert data["balance_usdc"] == pytest.approx(99_500.0, abs=1e-6)

    def test_deposit_staked_yield_increased_by_net(self, client, registered_wallet, sign_body):
        body = {
            "agent_id": registered_wallet["agent_id"],
            "wallet_address": registered_wallet["wallet_address"],
            "amount_usdc": 500.0,
        }
        sig = sign_body(registered_wallet["private_key"], body)
        r = client.post("/api/v1/bank/deposit", json=body, headers={"X-VectraFi-Signature": sig})
        assert r.status_code == 200
        data = r.json()
        expected_net = round(500.0 - round(500.0 * PROTOCOL_FEE_RATE, 8), 8)
        assert data["staked_yield_balance"] == pytest.approx(expected_net, abs=1e-6)

    def test_treasury_accumulates_across_deposits(self, client, registered_wallet, sign_body):
        amounts = [100.0, 200.0, 300.0]
        total_creator = 0.0
        total_bounty = 0.0

        for amount in amounts:
            body = {
                "agent_id": registered_wallet["agent_id"],
                "wallet_address": registered_wallet["wallet_address"],
                "amount_usdc": amount,
            }
            sig = sign_body(registered_wallet["private_key"], body)
            r = client.post("/api/v1/bank/deposit", json=body, headers={"X-VectraFi-Signature": sig})
            assert r.status_code == 200
            data = r.json()
            total_creator += round(amount * PROTOCOL_FEE_RATE * FEE_SPLIT_CREATOR_RATE, 8)
            total_bounty += round(amount * PROTOCOL_FEE_RATE * FEE_SPLIT_BOUNTY_RATE, 8)

        # Final treasury should reflect accumulated fees
        assert data["treasury_accumulated_fees_usdc"] == pytest.approx(total_creator, abs=1e-6)
        assert data["bounty_pool_accumulated_fees_usdc"] == pytest.approx(total_bounty, abs=1e-6)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestDepositEdgeCases:
    """Boundary and error-condition tests for deposits."""

    def test_zero_amount_rejected(self, client, registered_wallet, sign_body):
        body = {
            "agent_id": registered_wallet["agent_id"],
            "wallet_address": registered_wallet["wallet_address"],
            "amount_usdc": 0.0,
        }
        sig = sign_body(registered_wallet["private_key"], body)
        r = client.post("/api/v1/bank/deposit", json=body, headers={"X-VectraFi-Signature": sig})
        # Should be rejected (Pydantic validation: amount > 0)
        assert r.status_code in (400, 422)

    def test_negative_amount_rejected(self, client, registered_wallet, sign_body):
        body = {
            "agent_id": registered_wallet["agent_id"],
            "wallet_address": registered_wallet["wallet_address"],
            "amount_usdc": -100.0,
        }
        sig = sign_body(registered_wallet["private_key"], body)
        r = client.post("/api/v1/bank/deposit", json=body, headers={"X-VectraFi-Signature": sig})
        assert r.status_code in (400, 422)

    def test_insufficient_balance_rejected(self, client, registered_wallet, sign_body):
        body = {
            "agent_id": registered_wallet["agent_id"],
            "wallet_address": registered_wallet["wallet_address"],
            "amount_usdc": 200_000.0,  # More than 100k balance
        }
        sig = sign_body(registered_wallet["private_key"], body)
        r = client.post("/api/v1/bank/deposit", json=body, headers={"X-VectraFi-Signature": sig})
        assert r.status_code == 400

    def test_minimum_viable_deposit(self, client, registered_wallet, sign_body):
        body = {
            "agent_id": registered_wallet["agent_id"],
            "wallet_address": registered_wallet["wallet_address"],
            "amount_usdc": 0.000001,  # 1 micro-USDC
        }
        sig = sign_body(registered_wallet["private_key"], body)
        r = client.post("/api/v1/bank/deposit", json=body, headers={"X-VectraFi-Signature": sig})
        assert r.status_code == 200
        data = r.json()
        assert data["protocol_fee_usdc"] >= 0.0
        assert data["net_deposited_usdc"] >= 0.0


# ---------------------------------------------------------------------------
# Constants invariants
# ---------------------------------------------------------------------------


class TestProtocolInvariants:
    """Verify hardcoded protocol constants are not accidentally modified."""

    def test_fee_rate_is_0_25_percent(self):
        assert PROTOCOL_FEE_RATE == 0.0025

    def test_creator_rate_is_80_percent(self):
        assert FEE_SPLIT_CREATOR_RATE == 0.80

    def test_bounty_rate_is_20_percent(self):
        assert FEE_SPLIT_BOUNTY_RATE == 0.20

    def test_rates_sum_to_one(self):
        assert round(FEE_SPLIT_CREATOR_RATE + FEE_SPLIT_BOUNTY_RATE, 8) == 1.0

    def test_holding_addresses_are_placeholders(self):
        assert HOLDING_ADDRESS_USER == "0x0000000000000000000000000000000000000001"
        assert HOLDING_ADDRESS_BOUNTY == "0x0000000000000000000000000000000000000002"
