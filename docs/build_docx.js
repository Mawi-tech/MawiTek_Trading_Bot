// Generates MawiTek_Trading_Bot_Documentation.docx from the same content as
// ARCHITECTURE.md. Run with the global docx module on NODE_PATH:
//   NODE_PATH="$(npm root -g)" node build_docx.js
const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  AlignmentType, LevelFormat, TableOfContents, HeadingLevel, BorderStyle,
  WidthType, ShadingType, PageBreak, PageNumber, Header, Footer,
} = require("docx");

const CONTENT_W = 9360;                       // US Letter, 1" margins
const MONO = "Consolas";
const ACCENT = "2E5AAC";
const HEAD_FILL = "D5E8F0";

// ---- helpers ---------------------------------------------------------------
const H1 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(t)] });
const H2 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(t)] });
const H3 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_3, children: [new TextRun(t)] });

function P(text, opts = {}) {
  const runs = Array.isArray(text) ? text : [new TextRun({ text, ...opts.run })];
  return new Paragraph({ children: runs, spacing: { after: 120 }, ...opts.par });
}
const bold = (t) => new TextRun({ text: t, bold: true });
const run = (t) => new TextRun({ text: t });

function bullet(text) {
  const runs = Array.isArray(text) ? text : [new TextRun(text)];
  return new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: runs, spacing: { after: 60 } });
}

// Monospace "code" block line(s) — one Paragraph per line, light shaded.
function code(lines) {
  const arr = Array.isArray(lines) ? lines : [lines];
  return arr.map((ln, i) => new Paragraph({
    shading: { fill: "F2F4F7", type: ShadingType.CLEAR },
    spacing: { before: i === 0 ? 60 : 0, after: i === arr.length - 1 ? 120 : 0 },
    children: [new TextRun({ text: ln || " ", font: MONO, size: 18 })],
  }));
}

const border = { style: BorderStyle.SINGLE, size: 1, color: "C9D2DE" };
const borders = { top: border, bottom: border, left: border, right: border,
                  insideHorizontal: border, insideVertical: border };

function cell(text, w, { headerCell = false, mono = false } = {}) {
  const runs = (Array.isArray(text) ? text : [text]).map((t) =>
    typeof t === "string"
      ? new TextRun({ text: t, bold: headerCell, font: mono ? MONO : undefined, size: mono ? 18 : 20 })
      : t);
  return new TableCell({
    width: { size: w, type: WidthType.DXA },
    margins: { top: 60, bottom: 60, left: 110, right: 110 },
    shading: headerCell ? { fill: HEAD_FILL, type: ShadingType.CLEAR } : undefined,
    children: [new Paragraph({ children: runs })],
  });
}

// rows: array of arrays of cell-text; colWidths sums to CONTENT_W
function table(headers, rows, colWidths) {
  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map((h, i) => cell(h, colWidths[i], { headerCell: true })),
  });
  const bodyRows = rows.map((r) => new TableRow({
    children: r.map((c, i) => cell(c, colWidths[i], { mono: i === 0 && colWidths.length === 2 && colWidths[0] <= 3200 })),
  }));
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: colWidths,
    borders,
    rows: [headerRow, ...bodyRows],
  });
}

// ---- document content ------------------------------------------------------
const children = [];

// Title block
children.push(
  new Paragraph({ spacing: { before: 2400, after: 0 }, alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: "MawiTek Trading Bot", bold: true, size: 64, color: ACCENT })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 120, after: 0 },
    children: [new TextRun({ text: "Architecture & Calculations Reference", size: 32 })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 1200, after: 0 },
    children: [new TextRun({ text: "Multi-strategy options trading bot · Tradier integration", italics: true, size: 22, color: "666666" })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 120 },
    children: [new TextRun({ text: "Technical documentation — every module and every calculation", size: 20, color: "888888" })] }),
  new Paragraph({ children: [new PageBreak()] }),
);

// TOC
children.push(H1("Table of Contents"),
  new TableOfContents("Table of Contents", { hyperlink: true, headingStyleRange: "1-2" }),
  new Paragraph({ children: [new PageBreak()] }));

// 1. Design philosophy
children.push(H1("1. Design Philosophy"));
[
  [bold("Backtest-first. "), run("No directional edge goes live until it survives a backtest on real data, validated on at least two independent samples to reject overfits. Several experiments (HFT bidirectional, PEAD short puts, bear-call spreads, earnings IV-crush seller) were built, tested, and rejected.")],
  [bold("Fail open on data, fail closed on risk. "), run("A missing data read (IV, greeks, regime) never blocks trading; a breached risk limit (daily loss, allocation, concentration, vega) always does.")],
  [bold("One writer per state file. "), run("Each strategy process owns its own position book; cross-process shared files use atomic writes plus an advisory lock.")],
  [bold("Pure where possible. "), run("Signal detectors, scoring, P&L, and metrics are pure functions, unit-tested in MOCK_MODE with no network. Network wrappers are thin and fail soft.")],
].forEach((r) => children.push(bullet(r)));

// 2. Topology
children.push(H1("2. System Topology"));
children.push(P("start_all.py launches and supervises these long-running processes, each with its own market-hours guard and heartbeat:"));
children.push(table(
  ["Process", "File", "Role"],
  [
    ["Strategy 1 — Catalyst", "executor.py", "Long calls into an earnings/news catalyst (7–30 DTE)"],
    ["Strategy 2 — IV-Rank", "iv_rank_bot.py", "Premium selling — bull-put spreads & iron condors"],
    ["Strategy 3 — HFT", "hft_executor.py", "Intraday 0-DTE momentum scalps"],
    ["Strategy 4 — PEAD", "pead_executor.py", "Post-earnings / news-drift swings (14–35 DTE)"],
    ["Strategy 5 — Bounce", "bounce_executor.py", "Bear-regime capitulation-bounce longs"],
    ["News monitor", "news_feed.py", "Multi-source headlines + social-sentiment sweep"],
    ["Dashboard server", "dashboard_server.py", "Serves dashboard.html + JSON state"],
  ],
  [2400, 2400, 4560]));
children.push(P([bold("Data flow: "), run("scanners read market data (Tradier) → produce scored setups → the risk manager gates each candidate → order_manager places & confirms the real fill → the trade is recorded in that strategy's position book and the shared journal → dashboard_state.py assembles dashboard_state.json → dashboard.html polls and renders it.")]));

// 3. Strategies
children.push(H1("3. The Five Strategies"));
const strat = [
  ["Strategy 1 — Catalyst long calls", "executor.py, options_scanner.py",
   "Buys ~ATM/OTM long calls 7–30 DTE before an earnings/news catalyst. Exits via position_manager (TP/SL/DTE). Honestly modeled as negative-EV once IV crush is priced — it fights vega into the print; kept small. The real premium edge is Strategy 2."],
  ["Strategy 2 — IV-Rank premium selling", "iv_rank_bot.py",
   "Sells defined-risk premium when IV is rich. Preferred structure is a 4-leg iron condor (falls back to a bull-put spread). Buys the long protection wing(s) first (no naked-short risk on a partial fill), then sells the short leg(s). Exits: take profit at 50% of credit captured, stop at 2× credit, or time-stop at ≤7 DTE. The validated short-vol edge."],
  ["Strategy 3 — HFT intraday scalps", "hft_executor.py, hft_scanner.py",
   "Trades 0–2 DTE options on intraday momentum during 09:45–14:45 ET, flat by EOD (15:15 forced flatten). Uses a confluence of up to five signals (VWAP reclaim, ORB breakout, volume spike, range breakout, VWAP bounce). Asymmetric exits exploit convexity: TP +100% / SL −20% / 60-min time stop, plus trailing-stop + scale-out."],
  ["Strategy 4 — PEAD / news-drift", "pead_executor.py, pead_scanner.py",
   "Buys ~ATM options 14–35 DTE after a large, volume-confirmed gap that has held, riding the drift. Long-only, hard-gated to drift agreeing with the 50-day SMA slope (the trend gate is the whole edge). Exits: TP +80% / SL −35%, drift-fade, DTE ≤ 3, or 16-day max hold."],
  ["Strategy 5 — Capitulation bounce", "bounce_executor.py, bounce_scanner.py",
   "The bear-market offense: in a bear regime only, buys short-dated calls on oversold down-gaps (capitulation that snaps back). Same gap detector as PEAD but contrarian and hard-gated by market_regime — validated in bear samples, disastrous in bull."],
];
strat.forEach(([h, f, d]) => {
  children.push(H3(h));
  children.push(P([new TextRun({ text: f + " — ", italics: true, color: "666666" }), run(d)]));
});

// 4. Module reference
children.push(H1("4. Module Reference (every file)"));
const groups = [
  ["4.1 Orchestration & entry points", [
    ["start_all.py", "Process supervisor; launches every component, watches heartbeats, alerts on dead/stalled processes."],
    ["executor.py", "Strategy 1 loop; scans even when market is closed, gates only execution/monitoring to hours."],
    ["iv_rank_bot.py", "Strategy 2; own multi-leg book (locked), condor/spread selection & execution, exit monitor, reconcile."],
    ["hft_executor.py / pead_executor.py / bounce_executor.py", "Strategies 3–5; single-leg, share position_book.py + utils helpers."],
    ["options_scanner.py", "Strategy 1 scanner: earnings catalyst + optional news/flow, ranked by momentum."],
  ]],
  ["4.2 Scanners & signals", [
    ["hft_scanner.py", "Intraday signal engine; detectors + confluence floor + weighted score + conviction tiering."],
    ["pead_scanner.py", "Gap/drift detector (z-score, gap %, volume), trend + regime gates, drift scoring."],
    ["bounce_scanner.py", "Reuses the PEAD detector for oversold down-gaps, bear-regime contrarian long only."],
    ["momentum_scorer.py", "0–100 momentum score (volume surge, ROC, RSI, 52-week proximity)."],
    ["market_filter.py", "Liquidity screen (price/volume/$-volume) with a per-ET-day cache."],
    ["universe.py / screen_universe.py / update_universe.py", "Universe build, liquid pre-screen, and per-scanner rotation."],
  ]],
  ["4.3 Risk, regime & exits", [
    ["risk_manager.py", "Central gate: daily-loss halt, dup guard, concentration cap, vega cap, allocation clamp, bear throttle, IV sizing."],
    ["market_regime.py", "Single source of truth for bull/bear (SPY vs 200-day SMA), one fetch per ET day."],
    ["exit_manager.py", "Shared, pure trailing-stop + scale-out helpers."],
    ["portfolio_greeks.py", "Net book Δ/Γ/Θ/V aggregation, cached for the vega cap."],
  ]],
  ["4.4 Execution & broker", [
    ["tradier_client.py", "All Tradier REST calls; MOCK_MODE offline; consolidated get_option_mid / get_chain_greeks."],
    ["market_data.py", "OHLCV (daily/intraday) DataFrames + get_news."],
    ["order_manager.py", "place_and_confirm: place → poll to terminal state → record the real fill; crash-recovery ledger."],
    ["option_selector.py", "Generic liquid-contract picker (spread %, OI, bid/ask sanity)."],
    ["options_flow.py", "Optional unusual-options-activity signal."],
    ["position_manager.py", "Strategy 1 exit logic (TP/SL/DTE) + DTE helpers."],
  ]],
  ["4.5 Data providers", [
    ["earnings_provider.py / earnings_filter.py", "Next-earnings dates (yfinance + Yahoo API) with 24h cache; window filter."],
    ["news_catalyst.py", "Keyword sentiment scorer (bullish − bearish) used as a strategy score input."],
    ["news_sources.py", "Aggregates Tradier + Google News RSS + yfinance + SEC 8-K, dedups near-duplicate stories."],
    ["news_feed.py", "News-monitor process: categorize/score headlines, merge deduped, alert, trigger social sweep."],
    ["social_sentiment.py", "Per-ticker Stocktwits (bull/bear tags) + Reddit (volume + keyword) sentiment, combined."],
    ["iv_provider.py", "Per-ticker IV context: ATM IV, IV/HV, IV rank/percentile, regime."],
  ]],
  ["4.6 State, journaling & infra", [
    ["state_io.py", "atomic_write_json, read_json, file_lock, update_json; refuses Infinity/NaN."],
    ["position_book.py", "Shared single-leg book I/O for Strategies 3–5 (consolidated from 3 copies)."],
    ["trade_journal.py", "Appends closed trades (P&L, strategy, trade_type) to closed_trades.json."],
    ["decision_log.py", "Append-only per-decision audit (traded/rejected/exited), deduped."],
    ["equity_tracker.py", "Equity snapshots; marks open positions to market."],
    ["heartbeat.py / logger.py / utils.py", "Liveness beats; shared logger; timezone + spread_pct + is_market_open helpers."],
  ]],
  ["4.7 Analytics & dashboard", [
    ["analytics_metrics.py", "Sharpe, max drawdown, total return, win rate, profit factor, expectancy, breakdowns."],
    ["setup_tracker.py", "Forward returns + win/loss finalization → scanner hit-rate by score bucket."],
    ["dashboard_state.py", "Assembles dashboard_state.json (account, positions, metrics, panels, greeks, news, social)."],
    ["dashboard_server.py", "Hardened static server: allowlist, optional Basic Auth, security headers."],
    ["dashboard.html", "~2,400-line SPA (Overview, Strategies, News, Social, History, Decisions, Analytics); XSS-safe. Clickable strategy cards open a detail modal (description, calc, exits, stats, open positions, recent trades)."],
  ]],
  ["4.8 Safety & ops", [
    ["kill_switch.py", "Emergency flatten: cancel orders, market-close positions, set halt flag."],
    ["daily_report.py", "EOD digest (P&L, trades, halts, overnight exposure)."],
    ["event_notifier.py", "Fan-out alerts to Telegram / Discord / Email / SMS; logs events.json for the dashboard."],
    ["walk_forward.py", "In-sample vs out-of-sample degradation + live-vs-backtest divergence."],
    ["sandbox_validator.py / lifecycle_validator.py", "Exercise the real order path against the Tradier sandbox (gated)."],
  ]],
];
groups.forEach(([title, rows]) => {
  children.push(H3(title));
  children.push(table(["File", "Purpose"], rows, [3360, 6000]));
  children.push(P(""));
});
children.push(H3("4.9 Backtests (research scripts — untested, off the live path)"));
children.push(P("backtest.py (catalyst, honest IV-crush model), backtest_hft.py, backtest_iv_rank.py, backtest_pead.py, backtest_bounce.py, plus the rejected-experiment tools backtest_crush.py, backtest_bear_call.py, backtest_orb.py, backtest_vwap_bounce.py. They share a Black-Scholes pricing core (§5.15). Intentionally left duplicated rather than consolidated: they are untested dev tools that have diverged in small tuned ways (e.g. price floors), so merging them would risk silently changing research results with no test to catch it."));

// 5. Calculations
children.push(new Paragraph({ children: [new PageBreak()] }), H1("5. Calculations & Equations"));
children.push(P([new TextRun({ text: "Notation: ", italics: true }), run("S = spot, K = strike, T = years to expiry, σ/iv = implied vol (fraction), Φ = standard normal CDF. Option contract multiplier = 100.")]));

const eq = [
  ["5.1 Position sizing", [
    "budget    = min( equity × RISK_PER_TRADE_PCT , equity × MAX_POSITION_SIZE_PCT )   # 3% risk, 5% cap",
    "contracts = floor( budget / (mid_price × 100) )                                   # never oversize",
  ]],
  ["5.2 Per-strategy capital allocation", [
    "cap       = equity × STRATEGY_ALLOCATION_PCT[strategy]   # catalyst .40 / iv_rank .25 / hft .20 / pead .15",
    "remaining = cap − deployed_capital_by_strategy[strategy]",
    "allowed   = min( base_budget , remaining )              # reject if remaining ≤ 0",
  ]],
  ["5.3 Concentration cap", [
    "Each ticker maps to a correlation group (megacap_growth, semis, software, index, ...).",
    "Reject a new entry if its group already holds MAX_POSITIONS_PER_GROUP (3) names",
    "across ALL strategy books — so 'several tech names' can't masquerade as diversification.",
  ]],
  ["5.4 Bear-market throttle", [
    "budget ×= BEAR_SIZE_MULT      # 0.5  — halve per-trade budget   (bear regime only)",
    "cap    ×= BEAR_POSITION_MULT  # 0.6  — shrink the position-count cap",
    "# optionally pause new long-directional entries (BEAR_PAUSE_LONGS). Fails OPEN if regime unreadable.",
  ]],
  ["5.5 IV-aware sizing & IV context", [
    "budget ×= 0.6  if IV rich        (iv_rank ≥ 60  OR  iv/hv ≥ 1.30)",
    "budget ×= 0.4  if IV very rich   (iv_rank ≥ 85  OR  iv/hv ≥ 1.60)",
    "HV(20d)       = stdev(daily returns, last 20, sample) × √252        # annualized realized vol",
    "IV/HV ratio   = atm_iv / HV",
    "IV rank       = (iv − min(history)) / (max(history) − min(history)) × 100   # ≥10 readings",
    "IV percentile = count(history ≤ iv) / n × 100",
  ]],
  ["5.6 Momentum score (0–100)", [
    "Volume surge (25) = last_volume / mean(volume,20)   → 25/20/15/10/0 at ≥3.0/2.0/1.5/1.2×",
    "5-day ROC   (20)  = (close − close₋₅)/close₋₅ ×100   → 20/15/10/5/0  at ≥5/3/1.5/0%",
    "10-day ROC  (15)  = (close − close₋₁₀)/close₋₁₀×100  → 15/10/7/3/0   at ≥8/5/2/0%",
    "RSI trend   (20)  = 20 if RSI(14) rising AND 45≤RSI≤70; 10 if one; else 0",
    "52w proximity(20) = close / max(close,252) ×100      → 20/15/8/0     at ≥95/90/80%",
  ]],
  ["5.7 Intraday indicators (HFT)", [
    "typical_price = (High + Low + Close) / 3",
    "VWAP_t        = Σ(typical_price × Volume) / Σ(Volume)     # cumulative within the session (resets daily)",
    "RSI (Wilder)  = 100 − 100/(1+RS),  RS = avg_gain/avg_loss  (EWM, com = window−1)",
    "Confluence    = need ≥ HFT_MIN_CONFLUENCE of 5 core signals; emit only score ≥ MIN_SIGNAL_SCORE (45)",
    "Conviction    = high (full size) iff the VWAP+ORB+spike trio all fire; else relaxed (half size)",
  ]],
  ["5.8 PEAD/bounce drift event", [
    "move      = (close_event − close_prev) / close_prev",
    "move_z    = move / stdev(recent daily returns)            # how many σ the gap is",
    "vol_mult  = volume_event / mean(recent volume)",
    "qualifies ⇔ |move_z| ≥ 2.5  AND  |move| ≥ 4%  AND  vol_mult ≥ 1.8  AND the move has held",
    "direction = sign(move) → calls (up) / puts (down, where enabled)",
  ]],
  ["5.9 Net portfolio greeks", [
    "net_X      = Σ (leg_greek × signed_qty × 100)   for X in {delta, gamma, theta, vega}",
    "gross_vega = Σ |leg_vega|                        # size gauge; doesn't net long vs short",
    "Risk cap:  |net_vega| ≤ equity × MAX_PORTFOLIO_VEGA_PCT (1%)",
  ]],
  ["5.10 Trailing stop & scale-out (P&L as fractions)", [
    "peak          = max(peak_so_far, pnl)                              # high-water mark",
    "trailing hit ⇔ peak ≥ trail_activate AND pnl ≤ peak − trail_giveback",
    "scale-out qty = clamp( floor(qty × scale_fraction), 1, qty − 1 )  # once, when pnl ≥ scale_trigger",
  ]],
  ["5.11 News dedup & impact", [
    "Two headlines collapse when normalized titles match (lowercase, strip punctuation, truncate 80 chars).",
    "First source wins; others add to 'sources' and bump source_count.",
    "impact = category_weight + min(2, |sentiment|) + escalation_bonus → high ≥4, medium ≥2, else low",
  ]],
  ["5.12 Social sentiment", [
    "Stocktwits score = (bullish − bearish) / (bullish + bearish)        # untagged msgs keyword-scored",
    "Reddit score     = clamp( Σ keyword_sentiment / (mentions × 2), −1, +1 )",
    "combined         = volume-weighted mean of the two source scores",
    "net              = bullish if combined ≥ +0.15; bearish if ≤ −0.15; else neutral",
  ]],
  ["5.13 Performance metrics", [
    "daily return r  = (equityₜ − equityₜ₋₁) / equityₜ₋₁",
    "Sharpe (annual) = mean(r) / stdev(r, sample) × √252      # None if < 2 daily points",
    "max drawdown    = maxₜ ( (running_peak − equity) / running_peak )",
    "profit factor   = Σ wins / |Σ losses|                    # None if no losses (avoids Infinity)",
    "expectancy      = mean(pnl per trade);   win rate = wins / trade_count",
  ]],
  ["5.14 Scanner edge", [
    "forward_return % = direction_sign × (price_now − ref_price) / ref_price × 100",
    "outcome          = win if fwd ≥ +2%; loss if fwd ≤ −2%; else flat  (after the style horizon)",
    "hit_rate         = wins / (wins + losses)               # flats excluded; bucketed by setup score",
  ]],
  ["5.15 Option P&L and backtest pricing (Black-Scholes, r = 0)", [
    "Live long option:  pnl% = (exit_mid − entry_mid) / entry_mid",
    "Credit spread/condor: pnl$ = (entry_credit − cost_to_close) × 100 × qty",
    "                      cost_to_close = Σ short mids − Σ long mids",
    "d₁ = [ln(S/K) + ½σ²T] / (σ√T) ,   d₂ = d₁ − σ√T",
    "call = S·Φ(d₁) − K·Φ(d₂)          put = K·Φ(−d₂) − S·Φ(−d₁)",
    "delta_call = Φ(d₁)                gamma = φ(d₁)/(S·σ·√T)",
    "Φ(x) = ½(1 + erf(x/√2)).  Catalyst backtest models earnings IV crush (entry ~1.6×HV, exit ~0.9×HV).",
  ]],
  ["5.16 Account P&L summary & per-strategy attribution", [
    "realized   = Σ pnl_dollar over every closed trade in the journal     # uncapped; reconciles with metrics",
    "unrealized = Σ total_pnl_dollar over all open positions              # current mark-to-market",
    "total      = realized + unrealized ;   x_pct = x / start_equity ×100  # start = first equity snapshot",
    "Per-strategy split: tag each broker position by option symbol via the per-strategy books (the book's",
    "canonical strategy, NOT the structure name iv_rank records, e.g. bull_put_spread). Legs with no book",
    "match → 'unattributed', so the per-strategy split reconciles with the portfolio unrealized. The detail",
    "modal's recent-trades list filters closed trades by the same alias map (bull_put_spread/iron_condor → iv_rank).",
  ]],
];
eq.forEach(([title, lines]) => {
  children.push(H3(title));
  code(lines).forEach((p) => children.push(p));
});

// 6. State files
children.push(new Paragraph({ children: [new PageBreak()] }), H1("6. State Files (machine-local JSON; gitignored)"));
children.push(table(["File", "Writer", "Contents"], [
  ["risk_state.json", "risk_manager (locked)", "daily P&L, halt flag, trade counts"],
  ["open_positions.json", "executor", "Strategy 1 single-leg calls"],
  ["iv_rank_positions.json", "iv_rank_bot (locked)", "Strategy 2 multi-leg spreads/condors"],
  ["hft/pead/bounce_positions.json", "each executor", "Strategies 3–5 single-leg books"],
  ["pending_orders.json", "order_manager", "crash-recovery order ledger"],
  ["closed_trades.json", "trade_journal", "realized trades + P&L + tags"],
  ["equity_curve.json", "equity_tracker", "equity snapshots (Sharpe/drawdown source)"],
  ["decision_log.jsonl", "decision_log", "per-decision audit trail"],
  ["scanner_setups.json", "scanners (locked)", "accumulating scored setup board + tracking"],
  ["news_feed.json / social_sentiment.json", "news monitor", "dashboard News / Social feeds"],
  ["portfolio_greeks / iv_cache / iv_history.json", "greeks / iv_provider", "greeks + IV-context caches"],
  ["dashboard_state.json", "dashboard_state", "the dashboard's single source"],
  ["heartbeats/<name>.json", "each process", "liveness for the watchdog"],
], [3360, 2400, 3600]));

// 7. Testing & 8. Safety
children.push(H1("7. Testing"));
children.push(P("python -m pytest tests/ -q — 328 tests, all green, run in MOCK_MODE (no network). Coverage spans the risk gates, signal detectors, scoring, P&L (incl. the account P&L summary + per-strategy attribution), metrics, greeks, IV, exits, news/social parsing & aggregation, the dashboard panels, and the hardened server. Backtests are network research scripts and are not unit-tested."));

children.push(H1("8. Safety & Operations"));
[
  [bold("Kill switch: "), run("python kill_switch.py (typed FLATTEN) — cancel orders, flatten broker positions, halt.")],
  [bold("Remote dashboard: "), run("Tailscale HTTPS serve — see REMOTE_ACCESS.md.")],
  [bold("Going live (not yet ready): "), run("rotate the API key, set TRADIER_SANDBOX=false, and validate fills/partials on the first live session (confirm logic only exercised against MOCK + sandbox).")],
  [bold("Restart after upgrades: "), run("strategies load modules at process start, so code changes need Ctrl+C start_all → python start_all.py.")],
].forEach((r) => children.push(bullet(r)));

// ---- assemble --------------------------------------------------------------
const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 21 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 30, bold: true, color: ACCENT },
        paragraph: { spacing: { before: 280, after: 160 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 25, bold: true, color: "1F3864" },
        paragraph: { spacing: { before: 220, after: 120 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 22, bold: true, color: "333333" },
        paragraph: { spacing: { before: 180, after: 80 }, outlineLevel: 2 } },
    ],
  },
  numbering: { config: [
    { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 540, hanging: 280 } } } }] },
  ]},
  sections: [{
    properties: { page: {
      size: { width: 12240, height: 15840 },
      margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
    }},
    footers: { default: new Footer({ children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      children: [new TextRun({ text: "MawiTek Trading Bot — Architecture & Calculations Reference   ·   Page ", size: 16, color: "999999" }),
                 new TextRun({ children: [PageNumber.CURRENT], size: 16, color: "999999" })],
    })] }) },
    children,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync("MawiTek_Trading_Bot_Documentation.docx", buf);
  console.log("wrote MawiTek_Trading_Bot_Documentation.docx (" + buf.length + " bytes)");
});
