"""
Web3 bridge: maps internal escrow releases and ledger settlements onto live
ERC-20 transfers on a Layer 2 chain (Base Testnet / Base Mainnet).

Required environment variables (live mode only — omit for sandbox):
  L2_PROVIDER_URL           RPC gateway endpoint (e.g. https://sepolia.base.org)
  PROTOCOL_PRIVATE_KEY      Hex-encoded signing key for the platform's gas account
  USDC_CONTRACT_ADDRESS     ERC-20 USDC contract address on the target L2
  PLATFORM_TREASURY_ADDRESS Wallet that receives the 1.5% tax leg on every settlement

Usage (dry-run connectivity check):
  python core-exchange/src/web3_bridge.py --test-rpc
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import AsyncWeb3
from web3.exceptions import Web3Exception
from web3.providers import AsyncHTTPProvider

logger = logging.getLogger("vectrafi.web3_bridge")

# ---------------------------------------------------------------------------
# ERC-20 minimal ABI — only the transfer selector is needed for settlement.
# ---------------------------------------------------------------------------
_ERC20_TRANSFER_ABI: Final[list[dict]] = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Gas ceiling: if maxFeePerGas exceeds this (in gwei) the settlement is
# deferred to PENDING_SYNC rather than paying an unbounded gas cost.
_MAX_GAS_PRICE_GWEI: Final[int] = 500

# EIP-1559 tip sent to the block proposer; 1 gwei is sufficient on Base L2.
_MAX_PRIORITY_FEE_WEI: Final[int] = 1_000_000_000

# ERC-20 transfer uses ~65 000 gas on Base; 100 000 gives a comfortable margin.
_GAS_LIMIT: Final[int] = 100_000

# Default USDC decimal precision on Base (Circle's native USDC — 6 dp).
_USDC_DECIMALS_DEFAULT: Final[int] = 6


@dataclass
class OnchainSettlementResult:
    """Return value of process_onchain_settlement."""

    status: str  # CONFIRMING | PENDING_SYNC | FAILED
    settlement_mode: str = "escrow"  # "escrow" | "p2p"
    net_tx_hash: str | None = None
    tax_tx_hash: str | None = None
    error: str | None = None
    # wei values actually submitted (useful for audit logs)
    net_amount_wei: int = 0
    tax_amount_wei: int = 0
    max_fee_gwei: float = 0.0
    extra: dict = field(default_factory=dict)


class Web3BridgeError(Exception):
    """Raised for configuration or connectivity problems that are not retryable."""


class Web3Bridge:
    """
    Async bridge between VectraFi's internal ledger and ERC-20 transfers on L2.

    Two settlement modes (selected per-call by whether a sender private key is
    supplied to process_onchain_settlement):

      P2P mode   — the sender's own wallet signs Leg 1 (net → receiver), directly
                   debiting their on-chain USDC balance.  The protocol key signs
                   only Leg 2 (tax → treasury).  Zero-sum across participant wallets;
                   no escrow balance is consumed.

      Escrow mode (default) — both legs are signed by PROTOCOL_PRIVATE_KEY.  A
                   USDC balance preflight is executed before any broadcast: if the
                   escrow account holds fewer tokens than gross_amount the settlement
                   is deferred to PENDING_SYNC rather than draining the escrow blindly.

    Instantiate once at startup; reuse across requests (the AsyncWeb3 session is
    created lazily on first use and cached for the process lifetime).
    """

    def __init__(self) -> None:
        self.provider_url: str | None = os.getenv("L2_PROVIDER_URL")
        raw_pk = os.getenv("PROTOCOL_PRIVATE_KEY", "")
        self.usdc_address: str | None = os.getenv("USDC_CONTRACT_ADDRESS")
        self.treasury_address: str | None = os.getenv("PLATFORM_TREASURY_ADDRESS")

        self._account: LocalAccount | None = None
        if raw_pk:
            # Normalise: accept with or without leading 0x.
            pk = raw_pk if raw_pk.startswith("0x") else f"0x{raw_pk}"
            self._account = Account.from_key(pk)

        self._w3: AsyncWeb3 | None = None
        self._usdc_decimals: int | None = None

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """True when all four environment variables are present."""
        return bool(
            self.provider_url
            and self._account
            and self.usdc_address
            and self.treasury_address
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_w3(self) -> AsyncWeb3:
        """Return (and cache) an AsyncWeb3 instance connected to the RPC node."""
        if self._w3 is not None:
            return self._w3

        if not self.provider_url:
            raise Web3BridgeError("L2_PROVIDER_URL is not set")

        provider = AsyncHTTPProvider(
            self.provider_url,
            request_kwargs={"timeout": 15},
        )
        w3 = AsyncWeb3(provider)
        if not await w3.is_connected():
            raise Web3BridgeError(f"Cannot reach RPC endpoint: {self.provider_url}")
        self._w3 = w3
        return w3

    async def _usdc_decimals_cached(self, w3: AsyncWeb3) -> int:
        """Fetch and cache the USDC decimal count (almost always 6 on Base)."""
        if self._usdc_decimals is not None:
            return self._usdc_decimals

        contract = w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(self.usdc_address),  # type: ignore[arg-type]
            abi=_ERC20_TRANSFER_ABI,
        )
        try:
            self._usdc_decimals = await contract.functions.decimals().call()
        except Exception:
            self._usdc_decimals = _USDC_DECIMALS_DEFAULT
        return self._usdc_decimals

    def _to_wei(self, amount: Decimal, decimals: int) -> int:
        """Convert a human-readable USDC amount to the ERC-20 integer representation."""
        factor = Decimal(10) ** decimals
        return int((amount * factor).to_integral_value())

    async def _build_and_send_transfer(
        self,
        w3: AsyncWeb3,
        to_address: str,
        amount_wei: int,
        nonce: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
        chain_id: int,
        from_account: LocalAccount | None = None,
    ) -> str:
        """Build, sign, and broadcast a single ERC-20 transfer. Returns the tx hash.

        from_account: the LocalAccount that signs the transaction and pays gas.
        Defaults to self._account (the protocol escrow key) when None.
        """
        signer = from_account if from_account is not None else self._account
        if signer is None:
            raise Web3BridgeError("PROTOCOL_PRIVATE_KEY is not set")

        contract = w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(self.usdc_address),  # type: ignore[arg-type]
            abi=_ERC20_TRANSFER_ABI,
        )

        tx = await contract.functions.transfer(
            AsyncWeb3.to_checksum_address(to_address),
            amount_wei,
        ).build_transaction(
            {
                "from": signer.address,
                "nonce": nonce,
                "gas": _GAS_LIMIT,
                "maxFeePerGas": max_fee_per_gas,
                "maxPriorityFeePerGas": max_priority_fee_per_gas,
                "chainId": chain_id,
            }
        )

        signed = signer.sign_transaction(tx)
        tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    async def _get_usdc_balance_wei(self, w3: AsyncWeb3, address: str) -> int:
        """Return the on-chain ERC-20 USDC balance of address in token-unit integers."""
        contract = w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(self.usdc_address),  # type: ignore[arg-type]
            abi=_ERC20_TRANSFER_ABI,
        )
        return int(
            await contract.functions.balanceOf(
                AsyncWeb3.to_checksum_address(address)
            ).call()
        )

    # ------------------------------------------------------------------
    # Primary public interface
    # ------------------------------------------------------------------

    async def process_onchain_settlement(
        self,
        sender_wallet: str,
        receiver_wallet: str,
        gross_amount: Decimal,
        tax_amount: Decimal,
        sender_private_key: str | None = None,
    ) -> OnchainSettlementResult:
        """
        Submit two ERC-20 transfers to the L2 network:
          1. net_amount  → receiver_wallet
          2. tax_amount  → PLATFORM_TREASURY_ADDRESS

        Settlement mode is selected by whether sender_private_key is supplied:

          P2P mode  (sender_private_key provided):
            Leg 1 is signed by the *sender's own wallet* — their on-chain USDC
            balance is directly debited, making the settlement zero-sum across
            participant wallets.  Leg 2 (tax) is signed by the protocol key so
            the treasury always receives via the authorised platform account.
            No escrow balance is consumed in P2P mode.

          Escrow mode  (sender_private_key is None — default):
            Both legs are signed by PROTOCOL_PRIVATE_KEY.  A USDC balance
            preflight is executed before any broadcast: if the escrow account
            holds fewer tokens than gross_amount the call returns PENDING_SYNC
            with error "escrow_underfunded" rather than draining the account
            blindly.  This surfaces accounting gaps without causing fund loss.

        Transfers are sequential; true atomicity requires a custom settlement
        contract (not yet deployed).  If either transfer cannot be submitted —
        due to RPC timeout, connectivity loss, or gas price exceeding
        _MAX_GAS_PRICE_GWEI — the function returns status=PENDING_SYNC so the
        caller can persist that value and retry without double-committing the
        ledger debit.

        Args:
            sender_wallet:      On-chain address of the sending agent (audit log).
            receiver_wallet:    On-chain address of the receiving agent.
            gross_amount:       Gross USDC amount (before tax).
            tax_amount:         Tax portion in USDC (routed to treasury).
            sender_private_key: Hex-encoded private key of the sender's wallet.
                                When supplied the settlement runs in P2P mode.

        Returns:
            OnchainSettlementResult with status in {CONFIRMING, PENDING_SYNC, FAILED}.
        """
        if not self.is_configured:
            return OnchainSettlementResult(
                status="PENDING_SYNC",
                settlement_mode="escrow",
                error="Web3Bridge not fully configured — operating in sandbox mode",
            )

        net_amount = gross_amount - tax_amount

        # Resolve settlement mode and sender account.
        sender_account: LocalAccount | None = None
        settlement_mode = "escrow"
        if sender_private_key is not None:
            pk = (
                sender_private_key
                if sender_private_key.startswith("0x")
                else f"0x{sender_private_key}"
            )
            sender_account = Account.from_key(pk)
            settlement_mode = "p2p"

        # Pre-initialised so exception handlers can surface Leg 1's hash even
        # when Leg 2 fails after nonce N was already broadcast.
        net_tx_hash: str | None = None

        try:
            w3 = await self._get_w3()
            decimals = await self._usdc_decimals_cached(w3)
            net_wei = self._to_wei(net_amount, decimals)
            tax_wei = self._to_wei(tax_amount, decimals)
            gross_wei = self._to_wei(gross_amount, decimals)

            chain_id: int = await w3.eth.chain_id
            latest_block = await w3.eth.get_block("latest")
            base_fee: int = latest_block["baseFeePerGas"]
            max_priority_fee_per_gas: int = _MAX_PRIORITY_FEE_WEI
            max_fee_per_gas: int = base_fee * 2 + max_priority_fee_per_gas
            max_fee_gwei = float(AsyncWeb3.from_wei(max_fee_per_gas, "gwei"))

            logger.info(
                "onchain_settlement start mode=%s sender=%s receiver=%s "
                "gross=%s tax=%s net=%s chain_id=%s max_fee_gwei=%.2f",
                settlement_mode,
                sender_wallet,
                receiver_wallet,
                gross_amount,
                tax_amount,
                net_amount,
                chain_id,
                max_fee_gwei,
            )

            # Defer if gas is unreasonably expensive to prevent protocol loss.
            if max_fee_gwei > _MAX_GAS_PRICE_GWEI:
                logger.warning(
                    "Gas spike detected (%.2f gwei > %d gwei ceiling) — "
                    "deferring settlement to PENDING_SYNC",
                    max_fee_gwei,
                    _MAX_GAS_PRICE_GWEI,
                )
                return OnchainSettlementResult(
                    status="PENDING_SYNC",
                    settlement_mode=settlement_mode,
                    error=f"Max fee {max_fee_gwei:.2f} gwei exceeds ceiling",
                    max_fee_gwei=max_fee_gwei,
                    net_amount_wei=net_wei,
                    tax_amount_wei=tax_wei,
                )

            assert self._account is not None  # guarded by is_configured above

            if settlement_mode == "escrow":
                # Preflight: verify the escrow account holds enough on-chain USDC
                # to cover gross_amount before committing any broadcast.  Without
                # this the protocol wallet drains on every settlement leg regardless
                # of whether a matching deposit was ever received.
                escrow_usdc_wei = await self._get_usdc_balance_wei(
                    w3, self._account.address
                )
                if escrow_usdc_wei < gross_wei:
                    escrow_human = escrow_usdc_wei / (10**decimals)
                    logger.warning(
                        "escrow_underfunded sender=%s gross=%.8f USDC "
                        "escrow_balance=%.8f USDC — deferring to PENDING_SYNC",
                        sender_wallet,
                        float(gross_amount),
                        escrow_human,
                    )
                    return OnchainSettlementResult(
                        status="PENDING_SYNC",
                        settlement_mode="escrow",
                        error=(
                            f"escrow_underfunded: needs {float(gross_amount):.8f} USDC, "
                            f"holds {escrow_human:.8f} USDC"
                        ),
                        net_amount_wei=net_wei,
                        tax_amount_wei=tax_wei,
                        max_fee_gwei=max_fee_gwei,
                    )

                # Escrow mode: protocol account signs both legs (sequential nonces).
                leg1_account = self._account
                leg1_nonce: int = await w3.eth.get_transaction_count(
                    self._account.address, "pending"
                )
                leg2_account = self._account
                leg2_nonce = leg1_nonce + 1

            else:
                # P2P mode: sender signs Leg 1 (net → receiver) with their own key,
                # debiting their actual on-chain USDC balance.  Leg 2 (tax →
                # treasury) is signed by the protocol key — independent nonce
                # sequences because the two accounts are different wallets.
                assert sender_account is not None
                leg1_account = sender_account
                leg1_nonce = await w3.eth.get_transaction_count(
                    sender_account.address, "pending"
                )
                leg2_account = self._account
                leg2_nonce = await w3.eth.get_transaction_count(
                    self._account.address, "pending"
                )

            # Leg 1: net amount → receiver
            net_tx_hash = await self._build_and_send_transfer(
                w3,
                receiver_wallet,
                net_wei,
                nonce=leg1_nonce,
                max_fee_per_gas=max_fee_per_gas,
                max_priority_fee_per_gas=max_priority_fee_per_gas,
                chain_id=chain_id,
                from_account=leg1_account,
            )
            logger.info(
                "net_transfer submitted mode=%s tx=%s", settlement_mode, net_tx_hash
            )

            # Leg 2: tax amount → platform treasury
            tax_tx_hash = await self._build_and_send_transfer(
                w3,
                self.treasury_address,  # type: ignore[arg-type]
                tax_wei,
                nonce=leg2_nonce,
                max_fee_per_gas=max_fee_per_gas,
                max_priority_fee_per_gas=max_priority_fee_per_gas,
                chain_id=chain_id,
                from_account=leg2_account,
            )
            logger.info(
                "tax_transfer submitted mode=%s tx=%s", settlement_mode, tax_tx_hash
            )

            return OnchainSettlementResult(
                status="CONFIRMING",
                settlement_mode=settlement_mode,
                net_tx_hash=net_tx_hash,
                tax_tx_hash=tax_tx_hash,
                max_fee_gwei=max_fee_gwei,
                net_amount_wei=net_wei,
                tax_amount_wei=tax_wei,
            )

        except Web3BridgeError as exc:
            logger.error("Web3Bridge configuration error: %s", exc)
            return OnchainSettlementResult(
                status="PENDING_SYNC",
                settlement_mode=settlement_mode,
                net_tx_hash=net_tx_hash,
                error=str(exc),
            )

        except (Web3Exception, OSError, TimeoutError, asyncio.TimeoutError) as exc:
            # Network-level failures — ledger is already committed; flag for retry.
            # net_tx_hash is preserved so the recovery worker knows Leg 1 already
            # broadcast (nonce N consumed) and must not resubmit it.
            logger.warning(
                "RPC transport error during settlement — marking PENDING_SYNC: %s", exc
            )
            return OnchainSettlementResult(
                status="PENDING_SYNC",
                settlement_mode=settlement_mode,
                net_tx_hash=net_tx_hash,
                error=str(exc),
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in onchain settlement: %s", exc)
            return OnchainSettlementResult(
                status="FAILED",
                settlement_mode=settlement_mode,
                net_tx_hash=net_tx_hash,
                error=str(exc),
            )

    async def test_rpc_connection(self) -> dict:
        """
        Dry-run connectivity probe used by --test-rpc.
        Does not submit any transactions or read private state.
        """
        result: dict = {
            "provider_url": self.provider_url or "(not set)",
            "usdc_contract": self.usdc_address or "(not set)",
            "treasury_address": self.treasury_address or "(not set)",
            "signing_account": self._account.address if self._account else "(not set)",
            "connected": False,
            "chain_id": None,
            "latest_block": None,
            "gas_price_gwei": None,
            "usdc_decimals": None,
            "error": None,
        }

        if not self.provider_url:
            result["error"] = "L2_PROVIDER_URL is not set"
            return result

        try:
            w3 = await self._get_w3()
            result["connected"] = True
            result["chain_id"] = await w3.eth.chain_id
            result["latest_block"] = await w3.eth.block_number
            latest_block = await w3.eth.get_block("latest")
            base_fee_wei = latest_block.get("baseFeePerGas", 0)
            max_fee_wei = base_fee_wei * 2 + _MAX_PRIORITY_FEE_WEI
            result["gas_price_gwei"] = float(AsyncWeb3.from_wei(max_fee_wei, "gwei"))

            if self.usdc_address:
                result["usdc_decimals"] = await self._usdc_decimals_cached(w3)

        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)

        return result


# ---------------------------------------------------------------------------
# Module-level singleton — imported by settlement routes when live mode is on.
# ---------------------------------------------------------------------------
bridge = Web3Bridge()


# ---------------------------------------------------------------------------
# CLI: --test-rpc dry-run
# ---------------------------------------------------------------------------
async def _run_test_rpc() -> None:
    print("VectraFi Web3Bridge — RPC connectivity probe")
    print("=" * 52)
    probe = Web3Bridge()
    info = await probe.test_rpc_connection()

    for key, value in info.items():
        label = key.replace("_", " ").title()
        print(f"  {label:<22}: {value}")

    print("=" * 52)
    if info.get("connected"):
        print("  Result: OK — RPC endpoint reachable")
        sys.exit(0)
    else:
        print(f"  Result: FAIL — {info.get('error', 'unknown error')}")
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VectraFi Web3Bridge utilities")
    parser.add_argument(
        "--test-rpc",
        action="store_true",
        help="Probe the configured L2 RPC endpoint and exit",
    )
    args = parser.parse_args()

    if args.test_rpc:
        logging.basicConfig(level=logging.WARNING)
        asyncio.run(_run_test_rpc())
    else:
        parser.print_help()
        sys.exit(0)
