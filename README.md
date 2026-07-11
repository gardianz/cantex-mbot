# Cantex Bot

Multi-wallet interactive trading bot for the [Cantex](https://cantex.io) DEX,
built on the official `cantex_sdk`.

## Features

- **Multi-wallet** — one authenticated SDK session per wallet.
- **Interactive CLI** menu (`questionary`).
- **Strategy 1** — cycle USDCX ⇄ every USDCX pair, back and forth, until a daily
  per-wallet swap target is hit. Buy side is sized to the market value of *N* CC
  tokens; sell side dumps 100% of the token's unlocked balance. The daily target
  is counted from the **web trading history** (with the local counter as a
  fallback), so it stays in sync with what the exchange actually recorded.
- **Swap (pick & go)** — choose which wallets (one, several, or all), which
  USDCX-paired tokens, the direction, and an amount given as an absolute value
  or a **percent of balance** (e.g. `25%`, `100%`). One swap per wallet×token.
- **Guards** — every swap is rejected if slippage, pool fee, or network fee
  exceed configured limits. Cantex charges slippage + a pool fee (%) + a network
  fee (in CC); there is **no admin fee**. `max_network_fee` is also passed to the
  SDK as a hard cap on the live swap.
- **Telegram logging** — swap results, guard rejects, errors, summaries.
- **Scales to hundreds of wallets** — a background service refreshes a per-wallet
  cache with bounded concurrency (`max_concurrency`), and the dashboard paints
  from that cache instantly. Rows show `loading` until their first refresh lands.
- **Dashboard** (`rich`) — a portfolio summary (totals across all wallets) over a
  paged, scrollable per-wallet table (USDCX, CC, swaps t/24h/7d, fee 7d, rebate
  last-week + `Paid` status), plus a live LOG panel. Scroll with ↑↓/PgUp/PgDn,
  `g`/`G` top/end, `r` refresh, `q` back.
- **Instant menu, lazy auth** — the menu appears immediately; each wallet
  authenticates on first use (not all up front), so hundreds of unused wallets
  cost nothing.
- **No cookie, no browser** — every read is authenticated by the operator key.

## Where each number comes from

The official SDK has **no** endpoints for trading history, fees, or CC rebates
(verified against the SDK source and `api.cantex.io`). All of it is reachable
from the **operator key alone** — no session cookie, no browser, no Playwright.
Findings:

- **Trading history** — `GET api.cantex.io/v1/history/trading`, authenticated by
  the SDK's Bearer token. Powers the daily swap target and dashboard swap counts.
- **CC rebates** (weekly reward: yesterday / this week / last week, with a
  `Paid: <tx>` status once settled) — `GET api.cantex.io/v1/account/reward_activity`,
  also Bearer. This is the JSON the activity page renders; hitting it directly
  needs no cookie.
- **Trading fees** — the [ccview.io](https://ccview.io) Canton explorer
  (`/api/v1/internal/api/v1/parties/counterparties`), read over an **anonymous**
  ccview session (`GET /api/v1/session` sets the `sessionId` cookie it requires).
  The party id is public (`AccountInfo.address`). Fee = CC transferred OUT to
  `cantex.unverified.cns`.

All reads use `aiohttp`.

## Install

Requires **Python 3.11+** (SDK requirement).

```bash
python3.11 -m venv .venv
source .venv/bin/activate

pip install -e ../cantex_sdk          # the official SDK (not on PyPI)
pip install -e ".[dev]"               # this bot + test deps
```

## Configure

```bash
cp config.example.toml config.toml    # edit network, guards, strategy, wallets
cp .env.example .env                  # per-wallet keys + Telegram (never commit)
```

Each `[[wallets]]` block in `config.toml` needs matching entries in `.env`
(uppercased name):

- `CANTEX_<NAME>_OPERATOR_KEY` — required (Ed25519 operator key)
- `CANTEX_<NAME>_TRADING_KEY` — optional (secp256k1 intent key)

No cookies: history, rebates, and fees are all read with the operator key.

**`dry_run = true` is the default.** No real swaps are submitted — only quotes
and guard checks. Set `dry_run = false` (and arm `LIVE` in the CLI) only after
you have verified behaviour. This bot trades **real funds on mainnet**.

## Run

```bash
python -m cantex_bot          # or: cantex-bot
```

Menu: Dashboard · Strategy 1 · Swap 1× all pairs · Manual swap · Web check ·
Wallet status · Quit. The menu shows immediately (auth runs in the background);
live execution requires typing `LIVE` to arm.

**Web check** reads each wallet's history, weekly rebate, and on-chain fee — use
it to confirm the keys and data endpoints work before relying on them.

## Tests

```bash
pip install -e ".[dev]"
python -m pytest -v
```

Tests fake the SDK and web surfaces — no network, no keys, no cookies required.

## Notes & limits

- Strategy 1 stops hard at the daily target; a buy may be left un-sold (residual
  token inventory) if the target lands mid-cycle.
- The daily count is `max(web history today, web-at-start + this run's swaps,
  local counter)` to tolerate the exchange's indexing lag.
- Fees on the dashboard are **on-chain** (ccview), so they include activity from
  any source, not just this bot. The "Bot fee (local)" column is the bot's own
  recorded fees for reconciliation. ccview windows are date-based (UTC day
  granularity), so "24h" ≈ the last calendar day.
- CC rebates are the **weekly** reward; `this week` accrues until the week
  closes, `last week` shows `Paid: <tx>` once settled.
- Connection timeouts: raw connectivity to `api.cantex.io` is fine (~0.2s); rare
  stalls come from WSL2/NAT on a fresh connection. Mitigated with IPv4-pinned,
  DNS-cached, keep-alive sessions, generous connect timeouts, per-request
  retries, and the global concurrency cap — reads recover automatically.
- At scale, tune `[performance]` in `config.toml`: `max_concurrency` (raise for
  speed, lower to avoid rate-limits/timeouts) and `refresh_interval` (how often
  the background sweep re-reads every wallet). Logs (incl. per-wallet errors) go
  to `cantex_bot.log` and the dashboard LOG panel — `tail -f cantex_bot.log`.
- Never commit `.env`, `config.toml`, `secrets/`, or `state.db`.
