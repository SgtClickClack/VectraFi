import logging
import sys
from typing import Any

from web3 import Web3
from web3.exceptions import Web3Exception

from config import RPC_PROVIDER_URL, VAULT_ROUTING_ADDRESS

logger = logging.getLogger("vectrafi.web3")

_web3: Web3 | None = None
_live_mode: bool = False


def init_web3_provider() -> None:
    global _web3, _live_mode

    if not RPC_PROVIDER_URL:
        _web3 = None
        _live_mode = False
        logger.info("Web3 provider disabled — operating in local sandbox mode")
        return

    try:
        provider = Web3(Web3.HTTPProvider(RPC_PROVIDER_URL, request_kwargs={"timeout": 10}))
        if not provider.is_connected():
            raise Web3Exception("RPC provider connection check failed")

        chain_id = provider.eth.chain_id
        _web3 = provider
        _live_mode = True
        logger.info(
            "Web3 provider connected — chain_id=%s rpc=%s",
            chain_id,
            RPC_PROVIDER_URL,
        )
    except Exception as exc:
        # RPC_PROVIDER_URL was explicitly set but the connection failed.
        # Silently falling back to sandbox mode here would allow the swarm to
        # run with mock balances and a disabled gas guard — a silent fund-drain
        # risk in production.  Hard-exit instead so the failure is unmistakable.
        logger.critical(
            "FATAL: RPC_PROVIDER_URL is set (%s) but the Web3 provider could not "
            "connect — refusing to fall back to sandbox mode: %s",
            RPC_PROVIDER_URL,
            exc,
        )
        sys.exit(1)


def is_live_mode() -> bool:
    return _live_mode and _web3 is not None


def get_web3() -> Web3 | None:
    return _web3 if is_live_mode() else None


def get_on_chain_eth_balance(wallet_address: str) -> float:
    web3 = get_web3()
    if web3 is None:
        return 0.0

    balance_wei = web3.eth.get_balance(Web3.to_checksum_address(wallet_address))
    return float(web3.from_wei(balance_wei, "ether"))


def prepare_transaction_payload(
    wallet_address: str,
    operation: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    web3 = get_web3()
    if web3 is None:
        return {}

    checksum_address = Web3.to_checksum_address(wallet_address)
    nonce = web3.eth.get_transaction_count(checksum_address)
    chain_id = web3.eth.chain_id
    gas_price = web3.eth.gas_price

    payload: dict[str, Any] = {
        "operation": operation,
        "from": checksum_address,
        "to": VAULT_ROUTING_ADDRESS,
        "value": "0",
        "nonce": nonce,
        "chainId": chain_id,
        "gasPrice": str(gas_price),
        "gas": "21000",
        "status": "prepared_unsigned",
        "metadata": metadata,
    }

    logger.info(
        "Prepared on-chain transaction payload operation=%s from=%s nonce=%s chain_id=%s",
        operation,
        checksum_address,
        nonce,
        chain_id,
    )
    return payload
