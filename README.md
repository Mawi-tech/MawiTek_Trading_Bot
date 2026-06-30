# MawiTek Trading Bot

Multi-strategy options trading bot with a live dashboard, risk management, and broker integration via Tradier.

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-328%20passing-brightgreen)
![Broker](https://img.shields.io/badge/broker-Tradier-0a7cff)
![License](https://img.shields.io/badge/license-MIT-blue)

> ⚠️ **Disclaimer:** Educational / personal project. Trades against the Tradier **sandbox** (paper money) by default. Options trading carries substantial risk — nothing here is financial advice. Use at your own risk.

### Highlights

- **5 independent strategies run concurrently** — earnings catalyst, IV-rank premium selling (iron condors), 0-DTE intraday momentum, post-earnings drift, and a bear-regime capitulation bounce — each with its own position book, sizing, and exit logic.
- **Backtest-driven, not vibes-driven** — every strategy is validated against historical data before going live; experiments that lost money (bidirectional HFT signals, bearish drift, earnings IV-crush condors) were tested, **rejected, and documented** rather than shipped.
- **Production-grade safety** — real fill confirmation (no assumed mids), crash recovery via a pending-order ledger, atomic + cross-process-locked state files, an emergency kill switch, and a watchdog that detects dead *or* hung strategy processes.
- **Layered risk engine** — per-trade and daily-loss limits, per-strategy capital allocation, day/swing position budgets, a 9-cluster correlation cap, portfolio vega cap, IV-aware sizing, and an automatic bear-market throttle.
- **Live single-page dashboard** — equity, P&L, positions, scanner setups, multi-source news, retail social sentiment, a full decision audit log, and analytics (Sharpe, drawdown, profit factor, expectancy).
- **328 passing tests** that run fully offline in `MOCK_MODE` (no network, no broker calls).

**Stack:** Python · Tradier API · pandas/numpy · yfinance · vanilla-JS SPA dashboard · pytest

> **Full technical reference:** [ARCHITECTURE.md](docs/ARCHITECTURE.md) documents every file and every calculation (sizing, P&L, Greeks, VWAP/RSI, drift z-score, IV, sentiment, performance metrics) with the equations.

## Strategies

| # | Strategy | File | Description |
|---|---|---|---|
| 1 | **Catalyst Long Call** ⚠️ _retired_ | `executor.py` | Swing long calls into earnings (7–30 DTE). **Retired (negative-EV):** buying long premium into an event pays for IV crush — the backtest is ~32% win / negative P&L. New entries are disabled; the engine still exits any open positions. Its edge is captured better by Strategy 4 (post-earnings drift) and Strategy 2 (selling the crush). |
| 2 | **IV-Rank Premium** | `iv_rank_bot.py` | High IV → sell premium (iron condors, falls back to bull-put spreads); low IV → buy premium (long straddles). Fully exit-managed. |
| 3 | **HFT Intraday** | `hft_executor.py` | 0–1 DTE momentum scalps. Triggers: VWAP reclaim, ORB breakout, volume spike, range breakout, VWAP bounce, strong bar. Conviction-tiered and sized accordingly. |
| 4 | **PEAD / News Drift** | `pead_executor.py` | Post-earnings / news-driven gap drift. ATM 14–35 DTE long calls/puts on a confirmed daily-bar gap. Long-only (regime-gated shorts were tested and rejected). |
| 5 | **Capitulation Bounce** | `bounce_executor.py` | Bear-market offense. In a BEAR regime (SPY < 200d SMA), buys short-dated ATM calls into an oversold down-gap (the trade is the mean-reversion the four bearish-drift experiments failed to capture). Dormant in bull regimes. |

> **Day vs Swing.** Every trade is classified by structure via `classify_trade_type(strategy, dte)` — a ≤1-DTE contract is a **day** trade regardless of strategy; multi-day holds are **swing**. Day and swing get independent position budgets, and closed trades are tagged so the Analytics tab can compare the two.

> **Bear-market behaviour.** Strategy 5 is the dedicated bear-regime offense (exempt from the throttle below). Strategies 1, 2, 3, 4 are de-risked automatically in a bear regime — see `BEAR_REGIME_THROTTLE` in `risk_manager.py` (half size, ~40% fewer slots). `BEAR_PAUSE_LONGS=False` by default; flip it on to additionally pause the long-directional strategies (catalyst + PEAD) outright.

## File Structure

```
mawitek/                    Python package — run modules via `python -m mawitek.<sub>.<mod>`
  run/start_all.py          Launch all components + watchdog (the entry point)
  strategies/               Trading loops + signal scanners + contract selection
    executor.py             Strategy 1 — catalyst swing (RETIRED; exits only)
    iv_rank_bot.py          Strategy 2 — IV-rank premium (spreads/condors/straddles)
    hft_executor.py         Strategy 3 — intraday 0-DTE momentum
    hft_scanner.py          Intraday signal scanner (VWAP/ORB/spike/range/bounce)
    pead_executor.py        Strategy 4 — post-earnings / news drift
    pead_scanner.py         Daily-bar gap+drift detector
    bounce_executor.py      Strategy 5 — bear-regime capitulation bounce
    bounce_scanner.py       Regime-gated down-gap setup
    options_scanner.py      4-filter pipeline (earnings/flow/news/momentum)
    option_selector.py      Expiry + strike selection
  core/                     Risk, orders, positions, journaling, control
    risk_manager.py         Sizing, halts, allocation, caps, drawdown governor, retired/paused gates
    order_manager.py        Place + poll-to-fill, crash recovery
    position_manager.py     Catalyst exit logic + shared position I/O
    position_book.py        Shared single-leg book I/O (strategies 3-5)
    trade_journal.py        Closed-trade history
    decision_log.py         Why each trade taken/rejected (JSONL)
    equity_tracker.py       Mark-to-market equity snapshots
    exit_manager.py         Trailing-stop + scale-out helpers
    portfolio_greeks.py     Net Greeks (drives the vega cap)
    kill_switch.py          Emergency flatten-all
    bot_control.py          Runtime halt / pause-strategy control
    user_config.py          Account-size tiers + dashboard overrides
  data/                     Market data, feeds, providers, universe, broker
    tradier_client.py       Tradier API wrapper + option-mid pricing
    market_data.py          Daily/intraday bars + news
    market_filter.py        Liquidity filter (per-ET-day cache)
    market_regime.py        SPY vs 200d-SMA regime
    earnings_provider.py    Multi-source earnings API + cache
    earnings_filter.py      Earnings-window filter (thin wrapper)
    iv_provider.py          Per-ticker IV context
    news_feed.py            News monitor (multi-source + social)
    news_sources.py         Tradier + Google News + yfinance + SEC 8-K
    news_catalyst.py        Headline sentiment scoring
    social_sentiment.py     Stocktwits + Reddit sentiment
    options_flow.py         Bullish call-sweep detection
    momentum_scorer.py      Price/volume momentum (0-100)
    universe.py             Universe selection + rotation
    screen_universe.py      Pre-screen market -> liquid_universe.csv
    update_universe.py      Build sp500.csv / market_universe.csv
  dashboard/                Dashboard + public signal feed
    dashboard_state.py      Assembles dashboard_state.json
    dashboard_server.py     Static server (allowlist + security headers)
    signal_publisher.py     Sanitized public signal feed (signal service)
  analysis/                 Metrics + validation
    analytics_metrics.py    Sharpe, drawdown, profit factor, expectancy
    setup_tracker.py        Scanner hit-rate / forward returns
    walk_forward.py         In/out-of-sample + live-vs-backtest divergence
    lifecycle_validator.py  End-to-end paper round-trip (Tradier sandbox)
    sandbox_validator.py    Pre-flight API connection check
    daily_report.py         End-of-day digest
  infra/                    Cross-cutting utilities
    state_io.py             Atomic writes + cross-process file locks
    logger.py               Centralized rotating logging
    heartbeat.py            Per-strategy liveness signals
    utils.py                Timezone (ET) + helpers
    event_notifier.py       Telegram/Discord/email alerts + events feed

backtests/                  Strategy backtesters — run: `python -m backtests.<name>`
                            backtest_hft / iv_rank / pead / bounce / bear_call / crush / orb / vwap_bounce
tests/                      pytest suite (427 tests, MOCK_MODE, no network)
docs/                       ARCHITECTURE.md, CONTROL_API.md, REMOTE_ACCESS.md, build_docx.js
dashboard.html              Single-page dashboard (served by dashboard_server)
requirements.txt  README.md  LICENSE

```

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure API keys
Create `.env` in the project root:
```
TRADIER_API_KEY=your_key
TRADIER_ACCOUNT_ID=your_account
TRADIER_SANDBOX=true

# Optional: notifications
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
DISCORD_WEBHOOK_URL=...
```

### 3. Validate setup
```bash
python -m mawitek.analysis.sandbox_validator
```

### 4. Refresh the universe (first time + monthly)
```bash
python -m mawitek.data.update_universe       # writes sp500.csv (~503 names)
```

### 5. Run
```bash
# Single strategy
python -m mawitek.strategies.executor

# All five strategies + news monitor + dashboard + watchdog
python -m mawitek.run.start_all

# Dashboard only (separate terminal — use this server, NOT `python -m http.server`,
# because it enforces an allowlist that prevents serving .env and state files)
python -m mawitek.dashboard.dashboard_server
# Open http://localhost:8000/dashboard.html
```

## Risk Controls

| Control | Default | File |
|---|---|---|
| Risk per trade | 3% of equity | `risk_manager.py` → `RISK_PER_TRADE_PCT` |
| Daily loss limit (halt) | 5% of equity | `risk_manager.py` → `DAILY_LOSS_LIMIT_PCT` |
| Drawdown governor (profit protection) | graduated de-risk from the high-water mark — half size at −6%, quarter at −10%, **halt new entries at −13%**. Catches the slow multi-day bleed the daily halt resets through. HWM ratchets up and persists; anchors at current equity on first run. | `risk_manager.py` → `DRAWDOWN_GOVERNOR`, `DD_*_PCT` |
| Rolling weekly loss limit | halt new entries if trailing 5-trading-day P&L ≤ −8% | `risk_manager.py` → `WEEKLY_LOSS_LIMIT_PCT` |
| Max swing positions | 8 (catalyst + iv_rank + pead + bounce) | `risk_manager.py` → `MAX_SWING_POSITIONS` |
| Max day positions | 5 (hft intraday) | `risk_manager.py` → `MAX_DAY_POSITIONS` |
| Max total positions | 13 (swing + day) | `risk_manager.py` → `MAX_OPEN_POSITIONS` |
| Per-strategy capital cap | iv-rank 35 / pead 35 / hft 25 / bounce 15 (% of equity); catalyst **retired** | `risk_manager.py` → `STRATEGY_ALLOCATION_PCT`, `RETIRED_STRATEGIES` |
| Correlation cluster cap | ≤3 concurrent positions in any of 9 clusters (megacap_growth, index, semis, software, crypto, financials, energy, healthcare, consumer) | `risk_manager.py` → `MAX_POSITIONS_PER_GROUP` |
| Bear-regime throttle | half size, ~40% fewer slots when SPY < 200d SMA; bounce strategy is exempt | `risk_manager.py` → `BEAR_REGIME_THROTTLE` |
| IV-aware sizing | de-size long-premium buyers 0.6× when IV is rich / 0.4× very rich (rank ≥85 or IV/HV ≥1.6) | `risk_manager.py` → `IV_AWARE_SIZING` |
| Portfolio vega cap | block new vol exposure when \|net vega\| > 1% of equity per vol point | `risk_manager.py` → `MAX_PORTFOLIO_VEGA_PCT` |
| Take profit / stop loss (catalyst swing) | +100% / -50% | `position_manager.py` |
| Take profit / stop loss (HFT) | **+100% / -20%** (asymmetric — backtest-validated) | `hft_executor.py` |
| HFT confluence floor | **2-of-5 core signals** (vwap/orb/spike/range/bounce). Lowered from 3 (Jun 10 2026) for trade frequency — at 3 the scanner fired ~1 setup/day. Risk rails: relaxed conviction trades HALF size, tight -20% stop, daily-loss halt. | `hft_scanner.py` → `HFT_MIN_CONFLUENCE` |
| HFT prime session window | 9:45 AM – 2:45 PM ET (was 10:00–2:30) | `hft_scanner.py` → `PRIME_SESSION_*` |
| HFT conviction sizing | proven VWAP+ORB+spike trio 1.0% / relaxed 0.5% of equity | `hft_executor.py` → `HFT_SIZE_PCT_*` |
| HFT contract DTE | 0–1 preferred, falls back to ≤5 DTE for monthly-only names | `hft_executor.py` → `MAX_FALLBACK_DTE` |
| IV-rank entry-leg fill buffer | limit crosses 4% toward the far side (at-mid orders never filled) | `iv_rank_bot.py` → `LEG_FILL_BUFFER` |
| IV-rank credit exits (spreads + condors) | 50% of credit captured / 2× credit stop / ≤7 DTE | `iv_rank_bot.py` |
| IV-rank straddle exits | ±50% on debit / ≤7 DTE | `iv_rank_bot.py` |
| Prefer iron condor over bull-put | on (auto falls back if no call wing) | `iv_rank_bot.py` → `PREFER_IRON_CONDOR` |
| PEAD exits | +80% TP / −35% SL / drift-fade (gap fill) / DTE ≤3 / max-hold 16d | `pead_executor.py` |
| Bounce exits | +60% TP / −35% SL / DTE ≤2 / max-hold 9d | `bounce_executor.py` |
| Min setup score | 50 (catalyst) / 45 (HFT) / 55 (PEAD, Bounce) | per-executor file |

## Safety & Operations

| Tool | What it does |
|---|---|
| **Order fill confirmation** | `order_manager.py` polls the broker until an order truly fills, then records the **real** fill price/qty — not an assumed mid. Handles partial fills, rejections, and cancels the unfilled remainder on timeout. |
| **Crash recovery** | A pending-order ledger + `recover_pending_orders()` (run at each strategy's startup) resolve in-flight orders after a crash, so fills aren't lost or double-submitted. Recovered fills push a loud event-notifier alert. |
| **Concurrent-safe state** | `state_io.py` gives shared JSON files atomic writes + cross-process locks, so the five strategy processes never corrupt `risk_state.json`, `closed_trades.json`, etc. `allow_nan=False` rejects Infinity/NaN so the dashboard's `JSON.parse` can't break. |
| **Kill switch** | `python -m mawitek.core.kill_switch` cancels all orders, closes all positions at market, and sets the halt flag. Typed `FLATTEN` confirmation; `--force` to skip; `--status` for a dry run. |
| **Watchdog** | Each strategy writes a heartbeat; `start_all.py` alerts when a process dies **or** stalls (alive but hung, e.g. wedged network call). |
| **Daily summary** | `daily_report.py` sends an end-of-day digest (P&L, trades, halts, overnight exposure). Auto-fires after the close; or run `python -m mawitek.analysis.daily_report`. |
| **Timezone safety** | All "today" / "now" comparisons go through `utils.now_est()` / `utils.today_est()` — the bot agrees with the market on what day it is regardless of where the host runs. |
| **Dashboard server** | `dashboard_server.py` enforces a file allowlist (`.html / .css / .js / dashboard_state.json / backtest_equity.json / news_feed.json / social_sentiment.json`) so the static server can't accidentally serve `.env` or other state files. Optional HTTP Basic Auth (`DASH_AUTH_USER`/`DASH_AUTH_PASS`). Loopback-only by default. |

## Analysis Tools

```bash
python -m mawitek.analysis.analytics_metrics                          # portfolio metrics
python -m mawitek.analysis.walk_forward                               # overfitting check (in vs out-of-sample)
python -m mawitek.analysis.walk_forward --vs backtest_results.json    # live-vs-backtest divergence
python -m mawitek.data.earnings_provider lookup AAPL NVDA         # earnings dates (API + cache)
python -m backtests.backtest_hft --tickers SPY QQQ AAPL TSLA --days 30   # validate HFT triggers
python -m backtests.backtest_pead --days 730                              # validate PEAD strategy
python -m backtests.backtest_bounce                                       # validate the bounce strategy
python -m pytest tests/ -q                            # run the test suite (427 tests)
```

> The HFT backtest reports a **by-conviction** breakdown (proven VWAP+ORB+spike trio
> vs the looser "relaxed" setups) so you can see whether the looser triggers hold up
> before trusting them live. Re-run it after changing `HFT_MIN_CONFLUENCE` or the
> signal weights.

## Sandbox validation

Before trusting the bot with a live session, validate the full order lifecycle
against the Tradier **sandbox** (paper money):

```bash
python -m mawitek.analysis.lifecycle_validator          # read-only pre-flight (safe anytime)
python -m mawitek.analysis.lifecycle_validator --run    # full paper round-trip (asks to confirm)
```

The pre-flight checks credentials, sandbox mode, buying power, market hours, and
selects a liquid test contract — placing **no** orders. With `--run` (and a typed
`RUN` confirmation) it places one paper buy, confirms the real fill, records the
position, closes it, and verifies the journal — exercising the live order path
that mocks can't. Run it **during regular trading hours** (the sandbox returns
stale/crossed quotes when the market is closed). A `finally` block flattens the
test position even if a step fails. Refuses to run unless `TRADIER_SANDBOX=true`.

## Dashboard

Single-page dashboard at `http://localhost:8000/dashboard.html`:

- **Overview** — account equity, today's P&L, **all-time Total P&L** (realized + unrealized, with % return on starting capital), open positions (grouped by spread/single), scanner setups. Every setup carries a **Day trade / Swing** badge, a conviction tag, an IV-regime badge, a small-font signal-detail line, a forward-return badge, and a `style_reason` line explaining WHY it qualified; filter chips include Day trade and Swing, plus a **Recent / Score sort toggle**. Setups **accumulate (never deleted)** and show when they were first found + a "Live" badge when recently re-seen.
- **Strategies** — a **Portfolio P&L panel** (realized / unrealized / total, each as $ and % return), per-strategy heartbeat health, positions + capital usage vs allocation, **realized AND unrealized P&L per strategy**, portfolio correlation-cluster meter, market regime pill, portfolio-greeks strip, alert-channel status, recent-events feed. **Each strategy card is clickable** → a detail modal explaining what the strategy is, how it's calculated, its exit rules, current stats, the open positions it owns, and its recent trades.
- **News** — multi-source categorized headline feed (M&A, regulatory, earnings, hiring/firing, analyst, product, partnership, macro) aggregated & deduped from **Google News, Yahoo, SEC EDGAR 8-K and Tradier** for held positions, scanner setups, and a core watchlist. Collected every ~60s by the `news_feed.py` monitor; filter by category, impact, **source**, or ticker; duplicate stories show a "+N more" outlet badge. High-impact headlines also push to your notification channels.
- **Social** — per-ticker retail sentiment from **Stocktwits** (explicit bull/bear tags) and **Reddit** (mention volume + keyword sentiment), sorted most-discussed first, with a bull/bear gauge per name and a volume-weighted net-sentiment badge; filter bullish/bearish/neutral or by ticker.
- **Trade History** — closed trades with strategy badges, P&L, hold time, exit reasons; falls back to the broker's gain/loss API when the local journal is empty
- **Decision Log** — every decision from **all five strategies** (traded/rejected/considered/exited) with reasons, signals, conviction, and sizing; consecutive duplicates are collapsed
- **Analytics** — headline metrics (Sharpe, drawdown, profit factor, expectancy), equity curve, strategy comparison, day-vs-swing breakdown, score-to-outcome, rejection breakdown, hold-time scatter, scan-to-trade funnel

## State Files (auto-generated, gitignored)

| File | Purpose |
|---|---|
| `dashboard_state.json` | Dashboard reads this for live data |
| `open_positions.json` | Locally tracked open catalyst positions (Strategy 1) |
| `hft_positions.json` | Intraday HFT positions (Strategy 3, separate book) |
| `iv_rank_positions.json` | IV-rank multi-leg positions (Strategy 2, spreads/straddles/condors) |
| `pead_positions.json` | PEAD drift positions (Strategy 4) |
| `bounce_positions.json` | Capitulation-bounce positions (Strategy 5) |
| `closed_trades.json` | Trade history journal (all strategies) |
| `equity_curve.json` | Mark-to-market snapshots |
| `decision_log.jsonl` | Bot decision audit trail |
| `risk_state.json` | Daily P&L, halt flag, halt reason |
| `halt_events.json` | Daily-loss halt event log |
| `events.json` | Rolling feed of fills/closes/halts/big-moves shown on the dashboard |
| `news_feed.json` | Rolling deduped multi-source headline feed (served to the News tab) |
| `social_sentiment.json` | Per-ticker Stocktwits + Reddit sentiment (served to the Social tab) |
| `portfolio_greeks.json` / `iv_cache.json` / `iv_history.json` | Net-greeks + IV-context caches |
| `liquid_universe.csv` / `market_universe.csv` / `sp500.csv` | Screened + full + S&P universes |
| `earnings_cache.json` | Cached earnings dates (24h / 7d TTL) |
| `liquidity_cache.json` | Per-ET-day cached liquidity metrics (shared by all scanners) |
| `scanner_setups.json` | Accumulated scanner setups (Strategy 1) shown in the Overview tab |
| `pending_orders.json` | In-flight order ledger for crash recovery |
| `pnl_history.json` | Rolling 10-day P&L history for the dashboard |
| `last_summary.json` | Dedup marker for the daily summary |
| `heartbeats/` | Per-strategy liveness files for the watchdog |

## Notifications

Configure in `.env`. Events pushed to Telegram / Discord / Email / SMS (carrier email-to-SMS gateway):

- **Trade filled** — new position opened
- **Position closed** — exit with P&L
- **Daily halt** — loss limit breached
- **Big move** — position moves ±20%+ (deduped per threshold)
- **Trade setups** — fresh scanner candidates labelled **Day-trade** or **Swing** with the reason they qualified (score ≥60, batched per scan, one alert per ticker per day)
- **High-impact news** — market-moving headlines (M&A, regulatory, earnings, leadership changes) on watched tickers
- **Strategy DOWN** — a process exited or stalled (heartbeat went silent)
- **Recovered fill** — an order filled while the bot was offline (verify exit management!)

Every event also lands in `events.json` so the dashboard's Strategies tab shows a feed even when no notification channel is configured.

## License

Released under the [MIT License](LICENSE) — free to use, modify, and distribute with attribution.
