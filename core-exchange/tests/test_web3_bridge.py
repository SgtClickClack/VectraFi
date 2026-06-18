"""
Unit tests for core-exchange/src/web3_bridge.py

Covers:
  C01  is_configured is False when env vars are absent.
  C02  is_configured is True when all four env vars are present.
  C03  Private key without 0x prefix is normalised and accepted.

  W01  _to_wei: 1.0 USDC at 6 dp → 1_000_000 wei.
  W02  _to_wei: tax/net pair from a 100 USDC gross amount (1.5% tax).
  W03  _to_wei: sub-cent precision — 0.000001 USDC → 1 wei.
  W04  _to_wei: large amount — 10_000 USDC → 10_000_000_000 wei.

  P01  Unconfigured bridge fast-path → PENDING_SYNC, no network attempt.
  P02  TimeoutError during RPC connect → PENDING_SYNC.
  P03  asyncio.TimeoutError during RPC connect → PENDING_SYNC.
  P04  Web3Exception during RPC connect → PENDING_SYNC.
  P05  OSError (connection refused) during RPC connect → PENDING_SYNC.
  P06  Gas price above 500 gwei ceiling → PENDING_SYNC with error message.

  S01  Happy path → CONFIRMING with net and tax tx hashes returned.
  S02  Nonce sequencing: net leg uses N, tax leg uses N+1.
  S03  Correct address routing: net goes to receiver, tax to treasury.
  S04  Correct wei amounts passed to both transfer legs.
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from eth_account import Account
from web3 import AsyncWeb3
from web3.exceptions import Web3Exception

from web3_bridge import (
    OnchainSettlementResult,
    Web3Bridge,
    Web3BridgeError,
    _MAX_GAS_PRICE_GWEI,
    _USDC_DECIMALS_DEFAULT,
)

# ---------------------------------------------------------------------------
# Constants — deterministic throwaway credentials, never on any real network.
# ---------------------------------------------------------------------------
_TEST_PK       = "0x" + "11" * 32    # 64-char hex private key, all 0x11
_TEST_USDC     = "0x" + "33" * 20    # fake ERC-20 USDC contract address
_TEST_TREASURY = "0x" + "44" * 20    # fake platform treasury wallet
_TEST_RECEIVER = "0x" + "55" * 20    # fake agent receiver wallet
_TEST_PROVIDER = "https://sepolia.base.org"  # never contacted in unit tests

_TEST_ACCOUNT = Account.from_key(_TEST_PK)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _awaitable(val):
    """
    Return a fresh coroutine that yields *val* when awaited.

    Used to back AsyncWeb3 properties accessed as `await w3.eth.chain_id`
    (not called — just awaited directly), where AsyncMock alone is not
    sufficient because it is a *callable*, not a plain awaitable attribute.
    Each call produces a fresh coroutine so the mock can be used once per
    test without stale-coroutine issues.
    """
    async def _inner():
        return val
    return _inner()


def _make_bridge() -> Web3Bridge:
    """
    Return a fully configured Web3Bridge using test credentials.

    Bypasses __init__ (which reads env vars) so tests are hermetic.
    Pre-sets _usdc_decimals to avoid an RPC call for the decimals query.
    """
    bridge = Web3Bridge.__new__(Web3Bridge)
    bridge.provider_url     = _TEST_PROVIDER
    bridge._account         = _TEST_ACCOUNT
    bridge.usdc_address     = _TEST_USDC
    bridge.treasury_address = _TEST_TREASURY
    bridge._w3              = None
    bridge._usdc_decimals   = _USDC_DECIMALS_DEFAULT  # 6; skips the contract call
    return bridge


def _make_mock_w3(
    *,
    gas_price_gwei: float = 1.0,  # used as baseFeePerGas for EIP-1559 tests
    chain_id: int = 84532,   # Base Sepolia
    nonce: int = 42,
) -> MagicMock:
    """
    Build a MagicMock that satisfies all AsyncWeb3 attribute accesses made
    by process_onchain_settlement.

    chain_id is set as _awaitable() because the bridge accesses it with
    plain `await w3.eth.chain_id` (no call).
    get_block and get_transaction_count are callable+async so use AsyncMock.
    gas_price_gwei is interpreted as baseFeePerGas for EIP-1559 fee computation.
    """
    base_fee_wei = int(gas_price_gwei * 1e9)
    mock_w3 = MagicMock()
    mock_w3.eth.chain_id = _awaitable(chain_id)
    mock_w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": base_fee_wei})
    mock_w3.eth.get_transaction_count = AsyncMock(return_value=nonce)
    return mock_w3


def _run(coro):
    """Run an async coroutine in a fresh event loop (pytest-asyncio not required)."""
    return asyncio.run(coro)


def _settlement_args(
    gross: str = "100.0",
    tax: str   = "1.5",
) -> tuple:
    return (
        _TEST_ACCOUNT.address,  # sender_wallet (for audit log only)
        _TEST_RECEIVER,          # receiver_wallet
        Decimal(gross),          # gross_amount
        Decimal(tax),            # tax_amount
    )


# ---------------------------------------------------------------------------
# C-group: configuration mapping
# ---------------------------------------------------------------------------

class TestConfiguration:

    def test_C01_unconfigured_without_env(self):
        """All four env vars absent → is_configured is False."""
        empty = {
            "L2_PROVIDER_URL":          "",
            "PROTOCOL_PRIVATE_KEY":     "",
            "USDC_CONTRACT_ADDRESS":    "",
            "PLATFORM_TREASURY_ADDRESS": "",
        }
        with patch.dict(os.environ, empty):
            bridge = Web3Bridge()
        assert not bridge.is_configured

    def test_C02_configured_with_all_env(self):
        """All four env vars present → is_configured is True and fields populated."""
        env = {
            "L2_PROVIDER_URL":           _TEST_PROVIDER,
            "PROTOCOL_PRIVATE_KEY":      _TEST_PK,
            "USDC_CONTRACT_ADDRESS":     _TEST_USDC,
            "PLATFORM_TREASURY_ADDRESS": _TEST_TREASURY,
        }
        with patch.dict(os.environ, env):
            bridge = Web3Bridge()
        assert bridge.is_configured
        assert bridge.provider_url    == _TEST_PROVIDER
        assert bridge.usdc_address    == _TEST_USDC
        assert bridge.treasury_address == _TEST_TREASURY
        assert bridge._account is not None
        assert bridge._account.address == _TEST_ACCOUNT.address

    def test_C03_private_key_normalised_without_0x(self):
        """A private key supplied without the 0x prefix is silently normalised."""
        pk_no_prefix = "11" * 32   # 64 hex chars, no leading 0x
        env = {
            "L2_PROVIDER_URL":           _TEST_PROVIDER,
            "PROTOCOL_PRIVATE_KEY":      pk_no_prefix,
            "USDC_CONTRACT_ADDRESS":     _TEST_USDC,
            "PLATFORM_TREASURY_ADDRESS": _TEST_TREASURY,
        }
        with patch.dict(os.environ, env):
            bridge = Web3Bridge()
        assert bridge.is_configured
        # Address must match the known test key regardless of prefix convention
        assert bridge._account.address == _TEST_ACCOUNT.address


# ---------------------------------------------------------------------------
# W-group: _to_wei deterministic decimal → wei conversion
# ---------------------------------------------------------------------------

class TestToWei:

    def _bridge(self) -> Web3Bridge:
        return _make_bridge()

    def test_W01_one_usdc_at_6dp(self):
        """1.0 USDC with 6 decimal places → 1_000_000 wei."""
        assert self._bridge()._to_wei(Decimal("1.0"), 6) == 1_000_000

    def test_W02_standard_tax_net_pair_on_100_usdc(self):
        """
        Tax/net split for gross=100 USDC at 1.5% rate.
        The _apply_tax settlement function produces tax=1.5, net=98.5 with ROUND_UP.
        _to_wei must convert both amounts without loss for 6 dp USDC.
        """
        bridge = self._bridge()
        tax_wei = bridge._to_wei(Decimal("1.5"),  6)   # 1.5 * 10^6 = 1_500_000
        net_wei = bridge._to_wei(Decimal("98.5"), 6)   # 98.5 * 10^6 = 98_500_000
        assert tax_wei == 1_500_000
        assert net_wei == 98_500_000
        # Conservation: sum matches gross
        assert tax_wei + net_wei == bridge._to_wei(Decimal("100.0"), 6)

    def test_W03_sub_cent_precision(self):
        """0.000001 USDC (1 wei at 6 dp) — smallest transferable ERC-20 unit."""
        assert self._bridge()._to_wei(Decimal("0.000001"), 6) == 1

    def test_W04_large_amount(self):
        """10_000 USDC at 6 dp → 10_000_000_000 wei; no integer overflow."""
        assert self._bridge()._to_wei(Decimal("10000.0"), 6) == 10_000_000_000


# ---------------------------------------------------------------------------
# P-group: PENDING_SYNC fallback paths
# ---------------------------------------------------------------------------

class TestPendingSyncFallback:

    # ---- P01 ---------------------------------------------------------------

    def test_P01_unconfigured_bridge_returns_pending_sync(self):
        """
        When is_configured is False the bridge short-circuits immediately
        and returns PENDING_SYNC without attempting any network call.
        """
        empty = {k: "" for k in (
            "L2_PROVIDER_URL", "PROTOCOL_PRIVATE_KEY",
            "USDC_CONTRACT_ADDRESS", "PLATFORM_TREASURY_ADDRESS",
        )}
        with patch.dict(os.environ, empty):
            bridge = Web3Bridge()

        result = _run(bridge.process_onchain_settlement(*_settlement_args()))
        assert result.status == "PENDING_SYNC"
        assert result.net_tx_hash is None
        assert result.tax_tx_hash is None

    # ---- P02 ---------------------------------------------------------------

    def test_P02_stdlib_timeout_error_returns_pending_sync(self):
        """
        A stdlib TimeoutError from the RPC transport is caught and returns
        PENDING_SYNC so the worker loop is not interrupted.
        """
        bridge = _make_bridge()
        with patch.object(bridge, "_get_w3", side_effect=TimeoutError("connection timed out")):
            result = _run(bridge.process_onchain_settlement(*_settlement_args()))
        assert result.status == "PENDING_SYNC"
        assert "timed out" in (result.error or "")

    # ---- P03 ---------------------------------------------------------------

    def test_P03_asyncio_timeout_error_returns_pending_sync(self):
        """asyncio.TimeoutError (subclass of TimeoutError) is handled identically."""
        bridge = _make_bridge()
        with patch.object(bridge, "_get_w3", side_effect=asyncio.TimeoutError("rpc timeout")):
            result = _run(bridge.process_onchain_settlement(*_settlement_args()))
        assert result.status == "PENDING_SYNC"

    # ---- P04 ---------------------------------------------------------------

    def test_P04_web3_exception_returns_pending_sync(self):
        """A Web3Exception (e.g. provider unreachable) yields PENDING_SYNC."""
        bridge = _make_bridge()
        with patch.object(bridge, "_get_w3", side_effect=Web3Exception("provider not responding")):
            result = _run(bridge.process_onchain_settlement(*_settlement_args()))
        assert result.status == "PENDING_SYNC"

    # ---- P05 ---------------------------------------------------------------

    def test_P05_os_error_connection_refused_returns_pending_sync(self):
        """An OSError (ECONNREFUSED) from the transport layer yields PENDING_SYNC."""
        bridge = _make_bridge()
        with patch.object(bridge, "_get_w3", side_effect=OSError("connection refused")):
            result = _run(bridge.process_onchain_settlement(*_settlement_args()))
        assert result.status == "PENDING_SYNC"
        assert result.net_tx_hash is None
        assert result.tax_tx_hash is None

    # ---- P06 ---------------------------------------------------------------

    def test_P06_gas_spike_above_ceiling_returns_pending_sync(self):
        """
        When maxFeePerGas exceeds _MAX_GAS_PRICE_GWEI (500 gwei) the bridge
        defers the settlement to PENDING_SYNC to prevent unbounded gas loss.
        The error string must mention the fee so callers can log it.

        spike_base_fee=501 gwei → maxFeePerGas = 501*2 + 1 = 1003 gwei > 500.
        """
        spike_gwei = _MAX_GAS_PRICE_GWEI + 1   # 501 gwei base fee — ceiling is 500 gwei

        bridge   = _make_bridge()
        mock_w3  = _make_mock_w3(gas_price_gwei=spike_gwei)

        with patch.object(bridge, "_get_w3", new_callable=AsyncMock) as mock_get_w3:
            mock_get_w3.return_value = mock_w3
            result = _run(bridge.process_onchain_settlement(*_settlement_args()))

        assert result.status == "PENDING_SYNC"
        assert result.net_tx_hash is None
        assert result.tax_tx_hash is None
        # maxFeePerGas = baseFee*2 + 1 gwei priority = 501*2 + 1 = 1003 gwei
        expected_max_fee_gwei = spike_gwei * 2 + 1
        assert result.max_fee_gwei == pytest.approx(expected_max_fee_gwei, rel=1e-3)
        # Error message must reference the fee for observability
        assert "gwei" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# S-group: successful dual-leg settlement
# ---------------------------------------------------------------------------

class TestSuccessfulSettlement:
    """
    For the happy-path tests, _get_w3 is mocked to return a mock AsyncWeb3
    and _build_and_send_transfer is mocked to return canned tx hashes, so
    no real signing or network I/O occurs.
    """

    _NET_HASH = "0x" + "ab" * 32
    _TAX_HASH = "0x" + "cd" * 32
    _BASE_NONCE = 42

    def _run_happy_path(
        self,
        gross: str = "100.0",
        tax: str   = "1.5",
    ) -> tuple[OnchainSettlementResult, MagicMock]:
        """
        Execute process_onchain_settlement with both transport layers mocked.
        Returns (result, mock_send) so callers can inspect call args.
        """
        bridge  = _make_bridge()
        mock_w3 = _make_mock_w3(gas_price_gwei=1.0, nonce=self._BASE_NONCE)

        with patch.object(bridge, "_get_w3", new_callable=AsyncMock) as mock_get_w3, \
             patch.object(bridge, "_build_and_send_transfer", new_callable=AsyncMock) as mock_send:

            mock_get_w3.return_value      = mock_w3
            mock_send.side_effect         = [self._NET_HASH, self._TAX_HASH]

            result = _run(
                bridge.process_onchain_settlement(
                    _TEST_ACCOUNT.address,
                    _TEST_RECEIVER,
                    Decimal(gross),
                    Decimal(tax),
                )
            )

        return result, mock_send

    # ---- S01 ---------------------------------------------------------------

    def test_S01_happy_path_returns_confirming_with_hashes(self):
        """
        A successful dual-leg submission must return CONFIRMING and populate
        both net_tx_hash and tax_tx_hash from the mocked transfer calls.
        """
        result, _ = self._run_happy_path()
        assert result.status       == "CONFIRMING"
        assert result.net_tx_hash  == self._NET_HASH
        assert result.tax_tx_hash  == self._TAX_HASH
        assert result.error is None

    # ---- S02 ---------------------------------------------------------------

    def test_S02_nonce_sequencing_net_N_tax_N_plus_1(self):
        """
        The net-amount transfer must use nonce=N and the tax-amount transfer
        must use nonce=N+1.  This prevents the second transaction from being
        submitted before the first is mined (nonce ordering on L2 nodes).
        """
        _, mock_send = self._run_happy_path()

        assert mock_send.call_count == 2

        # nonce is passed as a keyword argument: _build_and_send_transfer(..., nonce=N, ...)
        first_call_nonce  = mock_send.call_args_list[0].kwargs["nonce"]
        second_call_nonce = mock_send.call_args_list[1].kwargs["nonce"]

        assert first_call_nonce  == self._BASE_NONCE
        assert second_call_nonce == self._BASE_NONCE + 1

    # ---- S03 ---------------------------------------------------------------

    def test_S03_correct_address_routing(self):
        """
        Leg 1 (net) must be addressed to the receiver wallet.
        Leg 2 (tax) must be addressed to the platform treasury.
        """
        _, mock_send = self._run_happy_path()

        # Arg index 1 = to_address
        net_recipient = mock_send.call_args_list[0].args[1]
        tax_recipient = mock_send.call_args_list[1].args[1]

        assert net_recipient == _TEST_RECEIVER
        assert tax_recipient == _TEST_TREASURY

    # ---- S04 ---------------------------------------------------------------

    def test_S04_correct_wei_amounts_in_both_legs(self):
        """
        The wei values passed to each transfer leg must be the deterministic
        result of _to_wei(net_amount, 6) and _to_wei(tax_amount, 6).
        Gross=100, tax=1.5, net=98.5 at 6 dp.
        """
        _, mock_send = self._run_happy_path(gross="100.0", tax="1.5")

        # Arg index 2 = amount_wei
        net_wei_sent = mock_send.call_args_list[0].args[2]
        tax_wei_sent = mock_send.call_args_list[1].args[2]

        assert net_wei_sent == 98_500_000   # 98.5 USDC × 10^6
        assert tax_wei_sent ==  1_500_000   #  1.5 USDC × 10^6
        # Conservation: sum equals gross
        assert net_wei_sent + tax_wei_sent == 100_000_000
