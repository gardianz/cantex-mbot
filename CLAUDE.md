# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`cantex-bot` — a multi-wallet interactive trading bot for the Cantex DEX. It trades **real funds on mainnet** by default (`config.toml` ships with `dry_run = false`, and live execution additionally requires typing `LIVE` in the CLI).

## Commands

Run everything from the repo root (`/home/gardianz/canton/cantex-mbot`).

```bash
pip install -e ../cantex_sdk       # the official SDK — NOT on PyPI, install first
pip install -e ".[dev]"            # this bot + pytest, pytest-asyncio, aioresponses
python -m pytest -q                # all tests (no network, keys, or cookies needed)
python -m pytest tests/test_bot.py::test_guard_rejects_high_slippage -v   # one test
python -m cantex_bot               # run the interactive CLI (or: cantex-bot)
tail -f cantex_bot.log             # logs never go to the console (see below)
```

Python 3.11+. `asyncio_mode = "auto"` is set in `pyproject.toml`, so async tests need no decorator. No lint/type-check is configured or enforced.

If `../cantex_sdk` isn't checked out: `pip install git+https://github.com/caviarnine/cantex_sdk.git`.

`config.toml` and `.env` are gitignored and absent from a fresh clone — copy from `config.example.toml` / `.env.example`. Tests fake the SDK and web surfaces, so they run without either.

## Architecture

`src/cantex_bot/`, one flat module per concern. [cli.py](src/cantex_bot/cli.py) `App` wires everything and owns the questionary menu; each menu item is an `action_*` coroutine.

### Every swap goes through one path

[swapper.py](src/cantex_bot/swapper.py) `SwapEngine.execute_swap` is the sole swap path — strategies, `swap 1x`, and manual swap all call it. Order is fixed: `ensure_auth → quote → record_fee → guard → (dry-run stop | live swap_and_confirm) → record_swap + incr_daily → notify`. New trading features should call it rather than `wallet.sdk.swap_and_confirm` directly; the fee observation, guard, counters, and Telegram notification all live there.

Two distinct escape hatches, don't conflate them:
- `bypass_guards` — ignore *all* limits (manual CLI override, real-money risk).
- `ignore_network_fee` — waive **only** `max_network_fee`, used when a round trip is already profitable enough that waiting out the fee risks more than it saves.

Either one also lifts the SDK-level `max_network_fee` cap passed to `swap_and_confirm`, or the SDK would reject a swap the guard just allowed.

### Guard units (easy to get wrong)

[guards.py](src/cantex_bot/guards.py): the Cantex API returns slippage and pool fee as **fractions** (`0.001`); config limits and `GuardResult.details` are in **percent** (`0.1` = 0.10%). `quote_metrics` does the ×100 and is the single source both the guard and the dashboard's PAIR FEES panel read. `max_network_fee` is absolute, in CC. Cantex charges slippage + pool fee (%) + network fee (CC) — there is **no admin fee**, despite `SwapQuote.fees.amount_admin` existing.

### Strategies — template method on `_pick`

[strategies/strategy1.py](src/cantex_bot/strategies/strategy1.py) holds the whole per-wallet loop (~580 lines): daily target, UTC rollover, loss brakes, ambiguous-swap reconciliation, adaptive fee polling. [strategy2.py](src/cantex_bot/strategies/strategy2.py) subclasses it and overrides **only `_pick`** (round-robin → lowest network fee, and never switch tokens while holding one). A new strategy in the same family should do the same: override `_pick`, set `name`/`label`, and change nothing else.

Loop invariants worth preserving:
- A wallet that hits its target, its loss budget, or repeated insufficient balance **idles until the next UTC day** rather than returning — returning would park it until *every* wallet returns, and a fee-polling wallet may never return.
- After a live swap whose confirmation *errored*, never fire the opposite leg on a maybe. `_confirm_via_history` polls the trading history: a higher today-count proves it settled. This is the buy/sell "collision" guard.
- The cycle-loss hold timer (`_held_since`) is cleared only when a position actually closes. When the timed stop-loss fires but the fee guard rejects the sell, the timer stays armed so the stop fires the moment the fee allows.

### Where each number comes from — three data sources

The SDK has no endpoints for trading history, fees, or rebates. All three are reachable from the **operator key alone** (no cookie, no browser):

| Data | Source | Auth |
|---|---|---|
| Balances, quotes, swaps, transfers | `cantex_sdk` | operator + intent keys |
| Trading history, CC rebates | [webclient.py](src/cantex_bot/webclient.py) → `api.cantex.io/v1/history/trading`, `/v1/account/reward_activity` | the SDK's Bearer token |
| On-chain trading fees | [ccview.py](src/cantex_bot/ccview.py) → ccview.io Canton explorer | anonymous ccview `sessionId` cookie |

`reward_activity` also carries the **period each reward covers** (`yesterday.date`, `this_week.start_datetime`) — take the fee/loss windows from those rather than computing them, so they stay right whatever the reward lag is. `this_week` is the trap: it reports the whole Mon–Sun period but has only accrued as far as `yesterday.date`, so charging it for costs incurred since drags PROFIT w negative. `PortfolioService._reward_windows` derives both windows and `profit_*` uses `fee_reward_day`/`loss_reward_week`, never the plain today/yesterday/week values.

The daily swap target counts from the **web history**, not the local counter — `_current_done` takes `max(web today, web-at-start + this run, local counter)` to tolerate the exchange's indexing lag.

`fetch_trading_history` pages by `offset`, and two rules keep the window whole: **advance by the rows returned, not `page_limit`** (the endpoint caps a page well below what you ask for), and **a short page is not the last page**. Getting either wrong truncates history to roughly one page — which only shows up once a wallet has done a full day of swaps, as the week's loss collapsing onto today's. `PortfolioService` caches the pages (`history_ttl`) and asks for only `_cover_days()`, back to this week's Monday.

`WebClient._loss_over` computes realised loss over complete `base→token→base` cycles with a **FIFO queue per token**, so a `base→A` buy is never closed by an unrelated `B→base` sell. Incomplete cycles are ignored. Positive = loss, negative = gain.

### Dashboard never fetches

[portfolio.py](src/cantex_bot/portfolio.py) `PortfolioService` runs a background sweep filling a per-wallet `WalletSnap` cache, bounded by `WalletManager.sem` (the global concurrency cap). [dashboard.py](src/cantex_bot/dashboard.py) paints **only** from that cache — a render fires on every keypress and tick, so an inline fetch or DB query there would be thousands of requests at scale. The heavy `pair_fee_stats` GROUP BY runs once per sweep via `asyncio.to_thread`; rows show `loading` until their first refresh lands.

[runstate.py](src/cantex_bot/runstate.py) `RunState` is the shared strategy↔dashboard channel: per-wallet `status`/`route`/`plan`/`done`/`target`, plus the active base symbol and selected tokens. `ROUTE` is the trade direction; `STATUS` is the phase.

### Supporting modules

- [wallets.py](src/cantex_bot/wallets.py) — one `CantexSDK` per wallet. Auth is **lazy** (`ensure_auth`), so hundreds of unused wallets cost nothing. `_seed_session` deliberately reaches into `sdk._session` to install the tuned connector.
- [nethelp.py](src/cantex_bot/nethelp.py) — IPv4-pinned, DNS-cached, keep-alive connector plus the shared timeout. Tuned for WSL2/NAT stalling a fresh SYN. `shared_connector()` is **one pool for the whole process** — every client (SDK sessions, web, ccview) borrows it with `connector_owner=False`, so a warm connection opened for one wallet serves the next and `limit`/`limit_per_host` are a real ceiling. A pool per wallet reuses nothing: each sweep would open a cold socket per wallet and eat the SYN stall. `_KEEPALIVE` must stay well above `refresh_interval`, or every pooled connection expires exactly when the next sweep needs it. `connector_for(proxy)` pools **per egress**, not per wallet — wallets sharing a proxy share warm connections; `shared_connector()` is just `connector_for(None)`.
- **Proxies.** The SDK builds its own requests and never passes `proxy=`, so a proxy is always applied at the *connector* — never per request. [proxies.py](src/cantex_bot/proxies.py) reads `proxies.txt` (gitignored; `proxies.example.txt` shows the five accepted line formats) and `assign()` maps line N to wallet N **by index**, wrapping when short, so a wallet keeps the same egress IP across restarts. Wired in `amain` before any session exists. A per-wallet proxy cannot use `HTTP(S)_PROXY` (that env var is process-wide), so a proxy list needs the `socks` extra even for `http://` entries — `aiohttp_socks.ProxyConnector` handles every scheme. The single-proxy path (`[network] proxy` / `CANTEX_PROXY`) still uses the env var for `http://`, which is why `configure_proxy()` and `trust_env=True` remain; a single `socks5://` falls through to the default pool instead. `[network] proxy_file` wins over `[network] proxy`. Never log a proxy URL raw — use `redact()`.
- [store.py](src/cantex_bot/store.py) — SQLite (`state.db`): swaps, daily counters, fee observations, scrape snapshots. Coarse-locked, thread-safe.
- [markets.py](src/cantex_bot/markets.py) — symbol↔`InstrumentId`. Every Cantex pool is CC↔token and the swap endpoint routes multi-hop at the same fee, so **`trade_pairs` does not require a direct base↔token pool** — any pool token is reachable from any base. (`usdcx_pairs` is the narrower legacy variant; `Pair.usdcx` is really "the base instrument".)
- [logging_setup.py](src/cantex_bot/logging_setup.py) — logs go to `cantex_bot.log` and an in-memory ring for the dashboard LOG panel, **never to the console**: a stray log line corrupts the questionary menu and the rich Live render. User-facing output is `console.print`. Records are attributed to a wallet by a `[name]` prefix or, failing that, the `wallet_logs()` ContextVar the per-wallet loops set — that is what lets SDK records (which know nothing about wallets) appear in the dashboard's per-wallet LOG panel. `_RetryNoiseFilter` drops the SDK's per-attempt retry WARNINGs; **`CANTEX_LOG_RETRIES=1` keeps them**, which is the only way to see what a `timed out after 4 attempts` actually was (connect stall vs slow read vs queued connection).
- [telegram.py](src/cantex_bot/telegram.py) — fire-and-forget notifier (never raises) plus a `getUpdates` long-poll command bot (`/stats`, `/help`), owner chat only.
- [preapproval.py](src/cantex_bot/preapproval.py) — Canton transfer pre-approvals, a fourth thing the SDK does not expose. Status is `tokens[].contracts.transfer_preapproval.contract_id` in the **raw** `GET /v1/account/admin` (the `AccountAdmin` model drops it); creating one posts `{instrumentAdmin, instrumentId}` to `/v1/ledger/transaction/build/transfer_preapproval` through the SDK's operator-key `_build_sign_submit`. One ledger transaction — and one network fee — per token per wallet, so already-active tokens are always skipped.
- [wallet_import.py](src/cantex_bot/wallet_import.py) — shared by `scripts/add_wallets.py` and the menu. `assert_gitignored` refuses to write secrets to a git-tracked file.

## Conventions

- **UTC everywhere** — including the dashboard clock, which is labelled `UTC` for a reason: a local clock next to UTC data reads as a different *date* (at WIB it showed the 20th while the rewards were on the 19th, making a normal one-day reward lag look like two). Daily counters, swap counts, loss windows, fee windows, and the strategy's day rollover all reset at 00:00 UTC — matching Cantex. A local-date boundary here is a bug (WIB = UTC+7 caused a real one). Weeks are Monday-start, matching Cantex reward periods.
- **Dashboard `plan` strings are Indonesian** ("saldo kurang", "proses swap", "tunggu rugi", "swap berhasil"). Match that when adding phases.
- **Word a loss, don't sign it.** The LOSS column reads negative as a *gain*, so a signed percentage in a status string means the opposite of the same sign in the table.
- **Per-wallet isolation.** Every sweep/batch catches per wallet (`except Exception  # noqa: BLE001`) so one bad wallet never aborts the rest. Keep that.
- New response data from the SDK is a frozen dataclass; parse via `_from_raw`, never construct from raw dicts.
- Never commit `.env`, `config.toml`, `secrets/`, `state.db`, or `*.log`.
- **README drift**: `README.md` says "`dry_run = true` is the default". That is the `NetworkConfig` code default, but `config.example.toml` ships `dry_run = false` — so a config copied from the example starts in live mode (still gated by the `LIVE` prompt). Trust the code and the example file.
