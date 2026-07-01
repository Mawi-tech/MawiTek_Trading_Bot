# Scanner Glossary

A plain-English reference for the signals, scores, and data sources the bot's
scanners use. Terms are grouped by where you'll see them (the dashboard's
Overview/Setups, the Decision Log, and the strategy code).

---

## Intraday signals (HFT scanner — `hft_scanner.py`)

These are the building blocks of an intraday (0–1 DTE) setup. Each is detected
on 5-minute bars within the current session and scored 0–100.

| Term | What it means |
|---|---|
| **VWAP** | Volume-Weighted Average Price — the average price weighted by volume since the open. The intraday "fair value" line; price above = bullish bias, below = bearish. |
| **VWAP reclaim** | Price crosses back **above** VWAP on rising volume after being below it — a bullish momentum trigger. |
| **VWAP bounce** | In an uptrend, price pulls back **to** VWAP, holds it as support, and turns up — a continuation entry. |
| **ORB (Opening Range Breakout)** | Price breaks above the high (or below the low) of the first part of the session (the "opening range"). A classic momentum trigger. |
| **Volume spike** | A bar trades at a large multiple of recent average volume — signals real participation behind a move. |
| **Range breakout** | Price breaks the high/low of the last ~12 bars (≈1 hour) on volume — a fresh directional push. |
| **Momentum burst** | A short-term surge measured with a fast RSI/ROC — confirms acceleration (used for scoring, not the confluence floor, because alone it's noisy). |
| **Strong bar** | A price-action conviction filter: the bar closes in the top (or bottom) ~30% of its range — buyers/sellers won the bar. |
| **Trend alignment** | The setup's direction agrees with the broader intraday trend (a filter, not a standalone trigger). |
| **Prime session** | The mid-session window (≈9:45 AM–2:45 PM ET) the bot trades; it avoids the noisy open and the EOD close. |

### How signals combine

| Term | What it means |
|---|---|
| **Composite score** | A weighted blend of the signals above (0–100). The proven signals (VWAP/ORB/spike) carry the heaviest weights. A setup must clear `MIN_SIGNAL_SCORE` to be considered. |
| **Confluence** | How many distinct core signals fire at once. The bot requires a **floor of 3** (`HFT_MIN_CONFLUENCE`) — single- or double-signal setups historically lost money. |
| **Conviction** | Quality tier of a setup: **high** = the proven VWAP+ORB+spike trio all fire (full size); **relaxed** = a different qualifying combo (half size). |

---

## Post-earnings / news-drift signals (PEAD scanner — `pead_scanner.py`)

Trades the *continuation* of a big news-driven move, **after** the gap.

| Term | What it means |
|---|---|
| **Drift** | The tendency of a stock to keep moving in the direction of a large catalyst (earnings/guidance/FDA/M&A) for days afterward. |
| **Event** | A qualifying catalyst day: an abnormal daily return (≥ a z-score threshold of recent volatility) **and** a minimum gap % **and** heavy volume. |
| **Gap** | The overnight/▲ jump in price from the catalyst. |
| **Volume multiple** | How many times normal volume the event traded — confirms it's real, not noise. |
| **Held fraction** | How much of the catalyst move has *stuck* since it happened — the bot only trades drift that held (didn't immediately fade). |
| **Trend gate** | Only trades drift that agrees with the 50-day SMA slope — backtests showed this trend alignment is the whole edge. |

---

## Earnings / flow / news (options scanner — Strategy 1, now retired)

Still referenced in the Decision Log for historical trades.

| Term | What it means |
|---|---|
| **Earnings catalyst** | An upcoming earnings date within the entry window (the original Strategy 1 trigger). |
| **Options flow** | Unusually large **call sweeps** (aggressive buying across exchanges) — a bullish-positioning signal. |
| **News catalyst** | A scored sentiment read on recent headlines for the ticker. |
| **Momentum score** | A 0–100 price/volume momentum rating used to rank candidates. |

---

## Implied volatility & options terms

| Term | What it means |
|---|---|
| **IV (Implied Volatility)** | The market's expected future volatility, baked into option prices. High IV = expensive options. |
| **IV Rank** | Where today's IV sits within its own 1-year range (0 = yearly low, 100 = yearly high). The IV-rank strategy **sells** premium when this is high. |
| **IV Percentile** | The % of days over the past year that IV was *below* today's level (similar intent to IV Rank). |
| **IV/HV ratio** | Implied vol ÷ historical (realized) vol. >1 means options are pricing in more movement than the stock has actually shown (rich). |
| **Rich / Cheap regime** | A label for whether options are expensive (rich → favor selling) or cheap (cheap → favor buying). |
| **IV crush** | The collapse of IV right after a known event (e.g., earnings). Why buying long premium *into* earnings is a structural loser — and why Strategy 1 was retired. |
| **DTE / 0-DTE** | Days To Expiration. 0-DTE = expires the same day (max gamma, max theta decay). |
| **ATM** | At-The-Money — strike ≈ the current stock price. |
| **Delta / Gamma / Theta / Vega** | Option "Greeks": sensitivity to price (Δ), to Δ itself (Γ), to time decay (Θ, the daily bleed), and to IV changes (V). |
| **Iron condor / Bull-put spread / Straddle** | Defined-risk option structures the IV-rank strategy sells (condor/spread) or buys (straddle). |
| **Bear-call spread** | The bearish mirror of the bull-put: sell an OTM call above the market, buy a higher call for protection, keep the credit if the stock stays flat or falls. The IV-rank fallback when the market is weak (gated behind `ENABLE_BEAR_CALL` until its backtest passes). |

---

## Exits, risk & portfolio terms

| Term | What it means |
|---|---|
| **TP / SL** | Take-Profit / Stop-Loss thresholds (on the option's value). HFT uses an **asymmetric** +100% TP / −20% SL — let winners run, cut losers fast. |
| **Trailing stop** | A stop that ratchets up as the trade gains, locking in profit. |
| **Scale-out** | Closing part of a position at a target while letting the rest run. |
| **Day vs Swing** | Classification by structure: ≤1-DTE = **day** trade; multi-day hold = **swing**. They get separate position budgets. |
| **Conviction sizing** | High-conviction setups trade full size; relaxed ones trade half. |
| **Correlation cluster** | A group of correlated tickers (e.g., megacap-growth, semis, index). The bot caps how many positions can stack into one cluster — 5 tech names is really *one* bet. |
| **Drawdown governor** | Protects profits: de-risks (half→quarter size) then halts new entries as equity falls from its high-water mark; also a rolling weekly loss limit. |
| **Daily loss limit / halt** | Stops new entries for the day if the account is down past a threshold. |
| **High-water mark (HWM)** | The peak equity reached; drawdown is measured from it. |
| **Bear-market throttle** | In a confirmed bear regime (SPY < 200-day SMA), new trades get half budget and fewer slots; small/micro tiers pause new long entries outright (`bear_pause_longs`). |
| **Red-day gate** | The intraday version of the bear throttle: SPY down ≥0.75% today halves new long budgets ("weak"); down ≥1.5% pauses new long entries ("red") until SPY recovers above −0.4%. Catches the sharp red session a once-a-day regime check can't see. |

---

## Data sources

| Source | Used for |
|---|---|
| **Tradier** | Broker API — account, positions, orders, real-time quotes, option chains (with Greeks), and intraday timesales. Sandbox for paper, production for live. |
| **yfinance** | Free historical price data (daily + intraday) and earnings dates — used by the backtests and as a data fallback. |
| **Google News (RSS)** | Per-ticker headline feed (the News tab + news-catalyst scoring). |
| **SEC EDGAR (8-K)** | Official material-event filings — the most authoritative catalyst source. |
| **Stocktwits** | Retail sentiment with explicit bull/bear tags (the Social tab). |
| **Reddit** | Mention volume + keyword sentiment for retail buzz (the Social tab). |
| **Wikipedia** | S&P 500 constituent list (universe building, via `update_universe.py`). |
| **Unusual Whales** *(optional)* | Options-flow data, if an API key is configured; otherwise flow signals are skipped. |

---

*See [ARCHITECTURE.md](ARCHITECTURE.md) for the full technical reference (formulas, file-by-file detail) and the project [README](../README.md) for setup and the strategy overview.*
