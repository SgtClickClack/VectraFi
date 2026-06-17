# VectraFi Project Guidelines

## Core Tech Stack
- Backend: FastAPI, Python 3.11+, SQLAlchemy, Uvicorn, HTTPX
- Cryptography: Web3.py, eth-account
- Framework Integration: OpenClaw Skills Registry (ClawHub Spec)

## System Architecture Strategy
- Dual-Mode: Must support offline SQLite/Mock Sandbox mode and Live Mainnet/Testnet RPC routing seamlessly via config checks.
- Zero Trust: All mutating requests (`/swap`, `/deposit`) MUST validate the `X-VectraFi-Signature` header against payload parameters.

## Code Standards
- Enforce strict Pydantic parsing schemas for all API inputs and outputs.
- Maintain independent routing engines under `core-exchange/src/routes/`.
- Never store an agent's private key anywhere in the database layer.

## Automated Contribution Workflow
- To propose changes, agents/contributors must submit clean PRs against feature-specific branches.
- Automated code reviews are evaluated via `anthropics/claude-code-action`.
- All modifications to core transaction rules require unit testing coverage verification.
