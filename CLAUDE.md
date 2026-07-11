# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Layout

The SDK lives in `cantex_sdk/` (its own git repo). Run all commands from there.

```bash
cd cantex_sdk
```

## Commands

```bash
pip install -e ".[dev]"                    # install with test deps (pytest, pytest-asyncio, aioresponses)
python -m pytest tests/ -v                  # run all tests
python -m pytest tests/test_cantex_sdk.py::TestOperatorKeySigner::test_from_hex -v   # single test
python examples/example.py                  # run example (needs CANTEX_* env vars, hits live API)
```

Requires Python 3.11+. No lint/type-check is configured in `pyproject.toml`; `ruff` and `mypy` caches are gitignored (used ad hoc, not enforced).

The example talks to a real Cantex environment and executes a swap (`swap_and_confirm` is uncommented). Don't run it against mainnet without intent. Set `CANTEX_BASE_URL` to testnet (`https://api.testnet.cantex.io`) for safe runs.

## Architecture

Whole SDK is one module: `src/cantex_sdk/_sdk.py` (~1900 lines). `__init__.py` re-exports everything via `from ._sdk import *` driven by `__all__`. Add new public symbols to `__all__`.

### Signer hierarchy (template-method pattern)

`BaseSigner` (ABC) owns all key-loading logic once: `from_hex`, `from_env`, `from_hex_file`, `from_raw_file`, `from_file`, `_clean_hex`. Subclasses implement only three hooks: `_from_key_bytes` (raw 32 bytes → signer), `from_pem_file`, `_to_pem`, plus `sign` / `get_public_key_hex`.

- **`OperatorKeySigner`** — Ed25519. Signs auth challenges AND ledger transaction hashes. `get_public_key_b64` (URL-safe, no padding) is the API auth identity.
- **`IntentTradingKeySigner`** — secp256k1 ECDSA. Signs pre-hashed 32-byte digests → DER signatures. `get_public_key_hex_der` produces the SPKI-wrapped key sent to `create_intent_trading_account`.

### Two write flows — the central abstraction

`CantexSDK._build_sign_submit(build_path, payload, *, intent=False)` unifies every mutating operation into build → sign → submit:

- **Operator flow** (`intent=False`): build ledger tx → sign `context.transaction_hash` (base64) with Ed25519 → submit to `/v1/ledger/transaction/submit`. Used by `transfer`, `batch_transfer`, `create_trading_account`, `reclaim_expired_transfer`, `reclaim_expired_allocation`.
- **Intent flow** (`intent=True`): build intent → sign `intent.digest` with secp256k1 → submit to `/v1/intent/submit`. Used by `swap`, `create_intent_trading_account`. Requires `intent_signer` or raises.

New mutating endpoints should route through `_build_sign_submit`, not raw `_request`.

### Response models

All frozen dataclasses. Each parses raw API JSON via a `_from_raw(cls, data)` classmethod — never construct from raw dicts directly, call `_from_raw`. `InstrumentId(admin, id)` is the shared value object; balances/quotes/pools all key on it. `SwapQuote` has deprecated flat properties (`trade_price`, `slippage`, ...) that warn and delegate to `prices.*` — use `prices.*`.

### WebSocket layer

- `CantexWebSocket` — async-iterable. Auto-answers ping with pong, swallows subscribe/unsubscribe acks, reconnects with exponential backoff on unexpected drops, and replays tracked subscriptions after reconnect.
- `_WebSocketConnect` — dual awaitable + async-context-manager returned by `connect_public_ws` / `connect_private_ws`.
- Event parsing: `_parse_ws_event` dispatches on `channel` suffix (`.ticker` → `TickerEvent`) then on API `type` string via the `_WS_EVENT_PARSERS` registry, defaulting to base `WsEvent` for unknown types. Event subclasses compose parent fields with `**Parent._from_raw(raw).__dict__`. To add an event type: define the dataclass, its `_from_raw`, and register the API type string in `_WS_EVENT_PARSERS`.
- `swap_and_confirm` opens the private WS *before* submitting so the confirmation event is never missed.

### HTTP & auth

`_request` retries 429/502/503/504 and network errors with exponential backoff (`max_retries`, `retry_base_delay`); 401/403 raise `CantexAuthError`. `authenticate` is a challenge-response flow (`/v1/auth/api-key/begin` → sign message → `/finish`), guarded by an asyncio lock; the API key is cached to disk at `api_key_path` (default `secrets/api_key.txt`, chmod 600) and revalidated with a probe request before reuse. Endpoints mix API versions: account/auth/ledger/intent are `v1`, pools/quote are `v2`.

## Gotchas

- **README drift**: `README.md` documents `transfer` / `batch_transfer` as taking separate `instrument_id` + `instrument_admin` args, but the code takes a single `instrument: InstrumentId`. The README also omits `create_trading_account`, `reclaim_expired_transfer`, and `reclaim_expired_allocation`. Trust the code over the README; update the README when changing these signatures.
- `subscribe` / `unsubscribe` reject a bare string and require an iterable of channel names.
