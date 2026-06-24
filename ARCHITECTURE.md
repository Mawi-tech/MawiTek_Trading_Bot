# MawiTek Trading Bot — Architecture & Calculations Reference

A multi-strategy, options-focused trading bot for the **Tradier** brokerage (paper/sandbox
as of this writing). It runs five independent strategies as separate processes, shares one
risk manager and one set of state files, and serves a single-page dashboard.

This document covers **how every file works** and **how every number is calculated** (with the
equations). For a quick start and operational notes see [README.md](README.md); for remote
access see [REMOTE_ACCESS.md](REMOTE_ACCESS.md).

---

## 1. Design philosophy

- **Backtest-first.** No directional edge goes live until it survives a backtest on real data,
  validated on at least two independent samples to reject overfits. Several experiments
  (HFT bidirectional, PEAD short puts, bear-call spreads, earnings IV-crush seller) were built,
  tested, and **rejected** — the rejections are documented in the code and the project memory.
- **Fail open on data, fail closed on risk.** A missing data read (IV, greeks, regime) must never
  block trading; a breached risk limit (daily loss, allocation, concentration, vega) always does.
- **One writer per state file.** Each strategy process owns its own position book; cross-process
  shared files use atomic writes + an advisory lock.
- **Pure where possible.** Signal detectors, scoring, P&L, and metrics are pure functions, unit-
  tested in `MOCK_MODE` with no network. Network wrappers are thin and fail soft.

---

## 2. System topology

`start_all.py` launches and supervises these long-running processes (each with its own market-hours
guard and heartbeat):

| Process | File | Role |
|---|---|---|
| Strategy 1 — Catalyst | `executor.py` | Long calls **into** an earnings/news catalyst (7–30 DTE) |
| Strategy 2 — IV-Rank | `iv_rank_bot.py` | Premium **selling** — bull-put spreads & iron condors |
| Strategy 3 — HFT | `hft_executor.py` | Intraday 0-DTE momentum scalps |
| Strategy 4 — PEAD | `pead_executor.py` | Post-earnings / news-drift swings (14–35 DTE) |
| Strategy 5 — Bounce | `bounce_executor.py` | Bear-regime capitulation-bounce longs |
| News monitor | `news_feed.py --monitor` | Multi-source headline feed + social-sentiment sweep |
| Dashboard server | `dashboard_server.py` | Static server for `dashboard.html` + JSON state |

**Data flow:** scanners read market data (Tradier) → produce scored *setups* → the risk manager
gates each candidate → `order_manager` places & confirms the real fill → the trade is recorded in
that strategy's position book and the shared journal → `dashboard_state.py` assembles everything
into `dashboard_state.json` → `dashboard.html` polls and renders it.

---

## 3. The five strategies (entry/exit logic)

### Strategy 1 — Catalyst long calls (`executor.py`, `options_scanner.py`)
Buys ~ATM/OTM **long calls 7–30 DTE before** an earnings or news catalyst, betting on a directional
move. Exits via `position_manager.py` (TP/SL/DTE). **Honestly modeled as negative-EV** once IV crush
is priced (see `backtest.py`) — it fights vega into the print. Kept for completeness and small sizing;
the real premium edge is Strategy 2.

### Strategy 2 — IV-Rank premium selling (`iv_rank_bot.py`)
Sells defined-risk premium when implied vol is rich. Preferred structure is a **4-leg iron condor**
(falls back to a **bull-put spread** if the call wing can't be built). Buys the long protection wing(s)
**first** (no naked-short risk on a partial fill), then sells the short leg(s). Own multi-leg book
(`iv_rank_positions.json`). Exits: take profit at **50% of credit captured**, stop at **2× credit**,
or time-stop at **≤7 DTE**. This is the validated short-vol edge (backtested PF and win-rate positive).

### Strategy 3 — HFT intraday scalps (`hft_executor.py`, `hft_scanner.py`)
Trades 0–2 DTE options on intraday momentum during the **09:45–14:45 ET** prime window, flat by EOD
(15:15 forced flatten). Uses a **confluence** of up to five signals (VWAP reclaim, ORB breakout, volume
spike, range breakout, VWAP bounce). Asymmetric exits exploit long-option convexity: **TP +100% / SL
−20% / 60-min time stop**, plus trailing-stop + scale-out (`exit_manager.py`). Marketable-limit entries
cross the spread so wide 0-DTE quotes still fill.

### Strategy 4 — PEAD / news-drift (`pead_executor.py`, `pead_scanner.py`)
Buys ~ATM options 14–35 DTE **after** a large, volume-confirmed gap that has *held*, riding the drift.
**Long-only**, hard-gated to drift that agrees with the 50-day SMA slope (the trend gate is the whole
edge). Exits: **TP +80% / SL −35%**, drift-fade (spot back through the pre-event close), DTE ≤ 3, or
16-calendar-day max hold.

### Strategy 5 — Capitulation bounce (`bounce_executor.py`, `bounce_scanner.py`)
The bear-market *offense*: in a **bear regime only**, buys short-dated calls on oversold down-gaps
(capitulation that snaps back). Same gap detector as PEAD but contrarian and regime-gated — validated
strongly in bear samples, disastrous in bull, so it is hard-gated by `market_regime`.

---

## 4. Module reference (every file)

### 4.1 Orchestration & entry points
- **`start_all.py`** — process supervisor. Launches each strategy + the news monitor + dashboard server
  using `sys.executable` with hardcoded args (no shell injection). Watches heartbeats and alerts on a
  dead **or** stalled (alive-but-hung) component via `event_notifier`.
- **`executor.py`** (Strategy 1) — scan → risk-check → place → journal loop; EOD digest after close;
  scans even when the market is closed (`bot_status="scanning_closed"`), only gating order execution and
  position monitoring to market hours.
- **`iv_rank_bot.py`** (Strategy 2) — the largest module; holds its own multi-leg book helpers
  (`_load/_save/_add/_remove/_update_iv_position`, atomic + locked), condor/spread selection & execution,
  the exit monitor, and `reconcile_iv_positions`.
- **`hft_executor.py` / `pead_executor.py` / `bounce_executor.py`** (Strategies 3–5) — single-leg
  executors. Their identical book logic now lives in **`position_book.py`** (below); each binds it to its
  own JSON file via thin wrappers and adds its own scan/monitor/reconcile/heartbeat.
- **`options_scanner.py`** — Strategy 1's scanner: filters the universe to names with a near-term
  earnings catalyst + optional news/flow confirmation and ranks by `momentum_scorer`.

### 4.2 Scanners & signals
- **`hft_scanner.py`** — intraday signal engine (§5.7). Detectors return `{signal, score, direction,
  detail}`; `scan_ticker` combines them via a confluence floor + weighted score; `hft_conviction`
  classifies high (proven VWAP+ORB+spike trio) vs relaxed (half-size). Bidirectional signals exist but
  are **off by default** (rejected in backtest).
- **`pead_scanner.py`** — gap/drift detector (§5.8): `detect_drift` qualifies an event by z-score, gap %,
  and volume multiple, requires the move to have held and to agree with the trend (and, in bear mode, the
  regime). `score_drift_setup` weights magnitude/volume/drift/recency.
- **`bounce_scanner.py`** — reuses the PEAD detector to find oversold down-gaps, but only in a bear regime
  and for the contrarian long.
- **`momentum_scorer.py`** — 0–100 momentum score (§5.6).
- **`market_filter.py`** — universe liquidity screen (price ≥ $5, ≥ 1M shares/day, ≥ $20M/day dollar
  volume), with a per-ET-day cache shared across strategies.
- **`universe.py` / `screen_universe.py` / `update_universe.py`** — universe management. `update_universe`
  builds `sp500.csv` (503 names) and `market_universe.csv` (~10.6k from the Nasdaq Trader directory);
  `screen_universe` pre-filters that to `liquid_universe.csv` (~1.1k tradable names); `universe.scan_csv()`
  picks the best available list and supports **per-scanner rotation offsets** so each strategy sweeps the
  market independently.

### 4.3 Risk, regime & exits
- **`risk_manager.py`** — the central gate. `pre_trade_check(ticker, strategy)` runs, in order:
  daily-loss halt → duplicate-position guard → concentration cap → net-vega cap → per-strategy allocation
  clamp → bear throttle → IV-aware sizing → final budget & contract count. Equations in §5.1–5.5.
- **`market_regime.py`** — single source of truth for bull/bear (SPY vs 200-day SMA), one fetch/ET-day,
  shared by the throttle, the PEAD/bounce gates, and the dashboard.
- **`exit_manager.py`** — shared, pure trailing-stop + scale-out helpers (§5.10).
- **`portfolio_greeks.py`** — net book Δ/Γ/Θ/V aggregation (§5.9), cached for the risk manager's vega cap.

### 4.4 Execution & broker
- **`tradier_client.py`** — all Tradier REST calls (quotes, chains, greeks, orders, balances). `MOCK_MODE`
  is on whenever no API key is set, so everything degrades to safe empties offline. Houses the consolidated
  `get_option_mid` and `get_chain_greeks`.
- **`market_data.py`** — OHLCV (daily/intraday) as DataFrames + `get_news`; the yfinance→Tradier migration
  layer for live data.
- **`order_manager.py`** — `place_and_confirm()` places an order then **polls to a terminal state** and
  records the *real* fill price/qty (handles partials, rejections, timeout-cancels). A pending-order ledger
  + `recover_pending_orders()` survive crashes; each order carries a unique idempotency `tag`.
- **`option_selector.py`** — generic liquid-contract picker (spread %, OI, bid/ask sanity).
- **`options_flow.py`** — optional unusual-options-activity signal (off without an Unusual Whales key).
- **`position_manager.py`** — Strategy 1's exit manager (TP/SL/DTE) and DTE helpers.

### 4.5 Data providers
- **`earnings_provider.py` / `earnings_filter.py`** — next-earnings dates via yfinance + Yahoo direct API
  with a 24h disk cache; the filter is a thin wrapper that answers "earnings within N days?".
- **`news_catalyst.py`** — keyword sentiment scorer (`score_headline` = bullish-minus-bearish keyword count)
  and `has_news_catalyst` used as a strategy score input.
- **`news_sources.py`** — multi-source headline layer: aggregates **Tradier + Google News RSS + yfinance +
  SEC EDGAR 8-K**, then dedups near-duplicate stories (§5.11). Pure parsers + fail-soft fetchers.
- **`news_feed.py`** — the news-monitor process: sweeps the focus tickers (held positions → setups →
  watchlist), categorizes/scores each headline, merges deduped into `news_feed.json`, alerts on high-impact
  items, and triggers the social sweep every few cycles.
- **`social_sentiment.py`** — per-ticker retail sentiment from **Stocktwits** (explicit bull/bear tags) and
  **Reddit** (mention volume + keyword sentiment), combined volume-weighted (§5.12) into
  `social_sentiment.json`.
- **`iv_provider.py`** — per-ticker IV context: ATM IV, IV/HV ratio, IV rank/percentile, regime (§5.5).

### 4.6 State, journaling & infra
- **`state_io.py`** — `atomic_write_json` (temp-file + `os.replace`), `read_json` (safe default), `file_lock`
  (cross-process advisory lock with stale-break), `update_json` (locked read-modify-write). Refuses to write
  `Infinity`/`NaN` (invalid JSON that breaks the dashboard parser).
- **`position_book.py`** — shared single-leg book logic (`load/save/add/remove/update`) for Strategies 3–5,
  bound per strategy to its own file. (Consolidated from three identical copies.)
- **`trade_journal.py`** — appends closed trades (with computed or supplied P&L, `strategy`, `trade_type`)
  to `closed_trades.json`; realized-P&L-today helper.
- **`decision_log.py`** — append-only audit of every scan decision (traded / rejected / exited) with reason,
  deduped per `(ticker, strategy)`.
- **`equity_tracker.py`** — equity snapshots to `equity_curve.json`; marks open positions to market via
  `get_option_mid`.
- **`heartbeat.py`** — `beat()` per loop into `heartbeats/<name>.json`; the watchdog reads these.
- **`logger.py`** — shared logger + `log_trade`.
- **`utils.py`** — timezone helpers (`now_est`, `today_est`, `parse_isodt` — everything trades on US/Eastern),
  `percent_change`, and the shared `spread_pct` / `is_market_open` helpers (§5).

### 4.7 Analytics & dashboard
- **`analytics_metrics.py`** — Sharpe, max drawdown, total return, win rate, profit factor, expectancy, and
  per-strategy / per-trade-type breakdowns (§5.13). Pure.
- **`setup_tracker.py`** — measures each surfaced setup's directional forward return and finalizes it as
  win/loss/flat after a per-style horizon, then reports scanner hit-rate by score bucket (§5.14).
- **`dashboard_state.py`** — assembles `dashboard_state.json`: account, the **cumulative P&L summary**
  (realized + unrealized + % return, §5.16), positions (grouped by underlying+expiry, each **tagged with its
  owning strategy** via `tag_positions_with_strategy` — a per-book option-symbol map, since broker positions
  don't carry a strategy), metrics, strategy panel (health / capital / **realized + unrealized P&L per
  strategy** / concentration / regime), greeks, events, alerts, news, social, and the tracked scanner board.
- **`dashboard_server.py`** — hardened static server: an **allowlist** (`_ALLOWED_EXTS` + `_ALLOWED_JSON`)
  ensures only dashboard assets and specific JSON files are served (never `.env` or a directory listing);
  optional HTTP Basic Auth; security headers.
- **`dashboard.html`** — ~2,400-line single-page app (Overview, Strategies, News, Social, Trade History,
  Decision Log, Analytics tabs); polls the JSON files; all rendering escapes via `esc()` (XSS-safe). Clicking a
  strategy card opens a detail modal — what it is, how it's calculated, its exits, current stats, the open
  positions it owns, and its recent trades.

### 4.8 Safety & ops
- **`kill_switch.py`** — emergency flatten: cancels all orders, market-closes all broker positions, sets the
  halt flag. CLI with typed `FLATTEN` confirm, `--force`, `--status`, `--no-halt`.
- **`daily_report.py`** — EOD digest (P&L, trades, halts, overnight exposure) via `event_notifier`.
- **`event_notifier.py`** — fan-out alerts to Telegram / Discord / Email / SMS (carrier email-to-SMS gateway);
  logs every event to `events.json` for the dashboard even with no channels configured.
- **`walk_forward.py`** — in-sample vs out-of-sample degradation + live-vs-backtest divergence checks.
- **`sandbox_validator.py` / `lifecycle_validator.py`** — exercise the real order path against the Tradier
  sandbox (place → confirm → record → close → reconcile), gated behind explicit `--run` confirmation.

### 4.9 Backtests (research scripts — not on the live path, untested)
`backtest.py` (catalyst, honest IV-crush model), `backtest_hft.py`, `backtest_iv_rank.py`, `backtest_pead.py`,
`backtest_bounce.py`, plus the "rejected experiment" tools `backtest_crush.py`, `backtest_bear_call.py`,
`backtest_orb.py`, `backtest_vwap_bounce.py`. They share a Black-Scholes pricing core (§5.15). **Intentionally
left duplicated** rather than consolidated: they are untested dev tools that have diverged in small tuned ways
(e.g. price floors), so merging them would risk silently changing research results with no test to catch it.

---

## 5. Calculations & equations

Notation: `S` = spot, `K` = strike, `T` = years to expiry, `σ`/`iv` = implied vol (fraction), `Φ` = standard
normal CDF. Option contract multiplier = 100.

### 5.1 Position sizing (`risk_manager.get_position_size`, `calculate_contracts`)
```
budget       = min( equity × RISK_PER_TRADE_PCT , equity × MAX_POSITION_SIZE_PCT )   # 3% risk, 5% cap
contracts    = floor( budget / (mid_price × 100) )                                   # never oversize
```

### 5.2 Per-strategy capital allocation (`_strategy_budget`)
Each strategy gets a slice of equity; the budget is clamped to what's left in that slice.
```
cap        = equity × STRATEGY_ALLOCATION_PCT[strategy]     # catalyst .40, iv_rank .25, hft .20, pead .15
remaining  = cap − deployed_capital_by_strategy[strategy]
allowed    = min( base_budget , remaining )                # reject if remaining ≤ 0
```
Position-count caps: `MAX_SWING_POSITIONS` + `MAX_DAY_POSITIONS` (day = DTE ≤ 1), summing to `MAX_OPEN_POSITIONS`.

### 5.3 Concentration cap (`concentration_reject`)
Each ticker maps to a `CORRELATION_GROUP` (megacap_growth, semis, software, index, crypto, …). A new entry is
rejected if its group already holds `MAX_POSITIONS_PER_GROUP` (3) names across **all** strategy books — so
"5 different tech names" can't masquerade as diversification.

### 5.4 Bear-market throttle (`_bear_throttle`)
In a bear regime (and unless the strategy is exempt):
```
budget  ×= BEAR_SIZE_MULT        # 0.5  — halve per-trade budget
cap     ×= BEAR_POSITION_MULT    # 0.6  — shrink the position-count cap
# optional: pause new long-directional entries entirely (BEAR_PAUSE_LONGS)
```
Fails **open** (no-op) if the regime can't be read.

### 5.5 IV-aware sizing & IV context (`_iv_size_mult`, `iv_provider`)
Long-premium buyers (catalyst/pead/bounce) are *de-sized* — not blocked — when IV is rich, grounded in the
crush result that buying expensive premium loses:
```
budget ×= 0.6  if IV rich           (iv_rank ≥ 60  OR  iv/hv ≥ 1.30)
budget ×= 0.4  if IV very rich       (iv_rank ≥ 85  OR  iv/hv ≥ 1.60)
```
IV context itself:
```
HV(20d)        = stdev(daily log-ish returns, last 20, sample) × √252          # annualized realized vol
IV/HV ratio    = atm_iv / HV
IV rank        = (iv − min(history)) / (max(history) − min(history)) × 100      # needs ≥10 readings
IV percentile  = count(history ≤ iv) / n × 100
regime         = rich if rank ≥ 60 (or ratio ≥ 1.30); cheap if rank ≤ 25 (or ratio ≤ 0.90); else normal
```

### 5.6 Momentum score (`momentum_scorer.score_momentum`) — 0–100
Sum of five tiered components:
```
Volume surge (25)  = last_volume / mean(volume, 20d)      → 25/20/15/10/0 at ≥3.0/2.0/1.5/1.2×
5-day ROC   (20)   = (close − close₋₅)  / close₋₅  × 100   → 20/15/10/5/0  at ≥5/3/1.5/0%
10-day ROC  (15)   = (close − close₋₁₀) / close₋₁₀ × 100   → 15/10/7/3/0   at ≥8/5/2/0%
RSI trend   (20)   = 20 if RSI(14) rising AND 45≤RSI≤70; 10 if one; else 0
52w proximity (20) = close / max(close,252d) × 100         → 20/15/8/0     at ≥95/90/80%
```

### 5.7 Intraday indicators (`hft_scanner`)
**Per-session VWAP** (reset each calendar day so the line matches the backtest):
```
typical_price = (High + Low + Close) / 3
VWAP_t        = Σ(typical_price × Volume)  /  Σ(Volume)     # cumulative within the session
```
**RSI (Wilder, 14)**: `avg_gain`/`avg_loss` are EWM with `com = window−1`; `RS = avg_gain/avg_loss`;
`RSI = 100 − 100/(1+RS)`.
**ORB**: breakout of the opening-range high/low (first N minutes) on volume.
**Confluence**: a setup needs at least `HFT_MIN_CONFLUENCE` of the 5 core signals; the composite score is a
weighted, direction-consistent sum, and only setups ≥ `MIN_SIGNAL_SCORE` (45) are emitted. **Conviction** is
*high* only when the proven VWAP+ORB+spike trio all fire (full size), else *relaxed* (half size).

### 5.8 PEAD/bounce drift event (`pead_scanner.detect_drift`)
```
move      = (close_event − close_prev) / close_prev
base_vol  = stdev(recent daily returns)
move_z    = move / base_vol                                # how many σ the gap is
vol_mult  = volume_event / mean(recent volume)
qualifies ⇔ |move_z| ≥ 2.5  AND  |move| ≥ 4%  AND  vol_mult ≥ 1.8  AND move has "held"
direction = sign(move)  → calls (up) / puts (down, only where enabled)
conviction= high (full size) if gap ≥ 4σ & ≥7% & ≥3× volume; else relaxed (half)
```

### 5.9 Net portfolio greeks (`portfolio_greeks.aggregate_greeks`)
Per leg, dollar greek = `greek × signed_quantity × 100` (long qty > 0, short < 0):
```
net_X     = Σ (leg_greek × qty × 100)     for X in {delta, gamma, theta, vega}
gross_vega= Σ |leg_vega|                  # size gauge that doesn't net long vs short
```
Interpretation: `net_delta` = $ P&L per +$1 underlying; `net_theta` = $ decay/day; `net_vega` = $ P&L per +1
vol point. The risk manager caps `|net_vega| ≤ equity × MAX_PORTFOLIO_VEGA_PCT` (1%).

### 5.10 Trailing stop & scale-out (`exit_manager`) — P&L as fractions
```
peak             = max(peak_so_far, pnl)                              # high-water mark
trailing hit  ⇔  peak ≥ trail_activate  AND  pnl ≤ peak − trail_giveback
scale-out qty    = clamp( floor(qty × scale_fraction), 1, qty − 1 )  # once, when pnl ≥ scale_trigger, qty ≥ 2
```
Per-strategy config, e.g. HFT/PEAD `(activate .40, giveback .25, trigger .50, fraction .5)`.

### 5.11 News dedup (`news_sources.dedup_articles`)
Two headlines collapse to one when their **normalized** titles match (lowercase, strip punctuation, collapse
whitespace, truncate to 80 chars). First source wins; the rest add to `sources` and bump `source_count`.
Headline **impact** = category weight + min(2, |sentiment|) + escalation-word bonus → high ≥ 4, medium ≥ 2,
else low.

### 5.12 Social sentiment (`social_sentiment`)
```
Stocktwits score = (bullish − bearish) / (bullish + bearish)         # explicit tags; untagged keyword-scored
Reddit score     = clamp( Σ keyword_sentiment / (mentions × 2), −1, +1 )
combined         = volume-weighted mean of the two source scores
net              = bullish if combined ≥ +0.15; bearish if ≤ −0.15; else neutral
volume           = stocktwits messages + reddit mentions             # board is sorted by this
```

### 5.13 Performance metrics (`analytics_metrics`)
```
daily return rₜ  = (equityₜ − equityₜ₋₁) / equityₜ₋₁
Sharpe (annual)  = mean(r) / stdev(r, sample) × √252                  # None if < 2 daily points
max drawdown     = maxₜ ( (running_peakₜ − equityₜ) / running_peakₜ )
total return     = (last − first) / first
profit factor    = Σ wins / |Σ losses|                               # None if no losses (avoids Infinity)
expectancy       = mean(pnl per trade)
win rate         = wins / trade_count
```

### 5.14 Scanner edge (`setup_tracker`)
```
forward_return % = direction_sign × (price_now − ref_price) / ref_price × 100
outcome          = win if fwd ≥ +2%; loss if fwd ≤ −2%; else flat   (finalized after the style horizon)
hit_rate         = wins / (wins + losses)                            # flats excluded; bucketed by setup score
```

### 5.15 Option P&L and backtest pricing
**Live long option** (catalyst/hft/pead/bounce): `pnl% = (exit_mid − entry_mid) / entry_mid`.
**Credit spread / iron condor** (iv_rank): credit collected up front; `pnl$ = (entry_credit − cost_to_close) ×
100 × qty`, where `cost_to_close = Σ short mids − Σ long mids`; profit when bought back below the credit.
**Backtests** price options with **Black-Scholes, r = 0**:
```
d₁ = [ ln(S/K) + ½σ²T ] / (σ√T) ,   d₂ = d₁ − σ√T
call = S·Φ(d₁) − K·Φ(d₂)            put = K·Φ(−d₂) − S·Φ(−d₁)
delta_call = Φ(d₁)                  gamma = φ(d₁) / (S·σ·√T)
Φ(x) = ½ (1 + erf(x/√2))            # at/after expiry, price = intrinsic value
```
The catalyst backtest additionally models **earnings IV crush** — entry priced at an inflated pre-earnings IV
(≈1.6× HV), exit re-priced at a crushed IV (≈0.9× HV) — which is what turns the naive "+50% winners" into the
honest negative-EV result.

### 5.16 Account P&L summary & per-strategy attribution (`dashboard_state`)
The Overview "Total P&L" card and the Strategies-tab Portfolio panel share one canonical figure
(`compute_pnl_summary`, pure):
```
realized   = Σ pnl_dollar  over every closed trade in the journal   (the same uncapped source the
             Strategies tab and Analytics metrics use, so the figures reconcile)
unrealized = Σ total_pnl_dollar  over all open positions             (current mark-to-market)
total      = realized + unrealized
x_pct      = x / start_equity × 100          # start_equity = first equity-curve snapshot; None if unknown
```
**Per-strategy split.** Broker positions don't say which strategy opened them, so
`tag_positions_with_strategy` builds a `{option_symbol → strategy}` map from each strategy's own position
book (keyed by the book's *canonical* strategy, **not** the per-record field — iv_rank books tag the
*structure*, e.g. `bull_put_spread`). Each position's unrealized P&L is then attributed to its strategy;
positions with no local-book match fall into an **`unattributed`** bucket, so the per-strategy split always
reconciles with the portfolio `unrealized`. Closed trades are filtered to a strategy with the same alias map
(structure names → `iv_rank`) for the per-strategy "recent trades" list in the detail modal.

---

## 6. State files (machine-local JSON; gitignored)

| File | Writer | Contents |
|---|---|---|
| `risk_state.json` | risk_manager (locked) | daily P&L, halt flag, trade counts |
| `open_positions.json` | executor | Strategy 1 single-leg calls |
| `iv_rank_positions.json` | iv_rank_bot (locked) | Strategy 2 multi-leg spreads/condors |
| `hft_positions.json` / `pead_positions.json` / `bounce_positions.json` | each executor | Strategies 3–5 single-leg books |
| `pending_orders.json` | order_manager | crash-recovery order ledger |
| `closed_trades.json` | trade_journal | realized trades + P&L + tags |
| `equity_curve.json` | equity_tracker | equity snapshots (Sharpe/drawdown source) |
| `decision_log.jsonl` | decision_log | per-decision audit trail |
| `scanner_setups.json` | dashboard_state / scanners (locked) | accumulating scored setup board + tracking |
| `news_feed.json` / `social_sentiment.json` | news monitor | dashboard News / Social feeds |
| `portfolio_greeks.json` / `iv_cache.json` / `iv_history.json` | greeks / iv_provider | greeks + IV context caches |
| `liquidity_cache.json` / `liquid_universe.csv` / `market_universe.csv` / `sp500.csv` | filters / universe | universe + liquidity caches |
| `dashboard_state.json` | dashboard_state | the dashboard's single source |
| `heartbeats/<name>.json` | each process | liveness for the watchdog |

---

## 7. Testing

`python -m pytest tests/ -q` — **328 tests**, all green, run in `MOCK_MODE` (no network). Coverage spans the
risk gates, signal detectors, scoring, P&L, metrics, greeks, IV, exits, news/social parsing & aggregation,
the dashboard panels, and the hardened server. Backtests are network research scripts and are not unit-tested.

---

## 8. Safety & operations

- **Kill switch:** `python kill_switch.py` (typed `FLATTEN`) — cancel orders, flatten broker positions, halt.
- **Remote dashboard:** Tailscale HTTPS serve — see [REMOTE_ACCESS.md](REMOTE_ACCESS.md).
- **Going live (not yet ready):** rotate the API key, set `TRADIER_SANDBOX=false`, and validate fills/partials
  on the first live session (the confirm logic has only been exercised against MOCK + sandbox).
- **Restart after upgrades:** strategies load their modules at process start, so any code change needs
  `Ctrl+C start_all` → `python start_all.py` to take effect.
