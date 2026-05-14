# Options Catalyst Bot

Automated long call scanner and executor for earnings + news catalyst plays.

## File Structure

```
├── executor.py          ← Main bot loop (run this)
├── options_scanner.py   ← Full 4-filter scanner pipeline
├── earnings_filter.py   ← Earnings within 1-5 days
├── options_flow.py      ← Unusual Whales call sweep detection
├── news_catalyst.py     ← News headline sentiment scoring
├── momentum_scorer.py   ← 0-100 price/volume momentum score
├── option_selector.py   ← Best expiry + strike selection
├── risk_manager.py      ← Position sizing + daily loss limit
├── position_manager.py  ← Exit logic (TP/SL/expiry/post-earnings)
├── tradier_client.py    ← Tradier API wrapper
├── bot.py               ← Original stock bot (unchanged)
├── scanner.py           ← Original scanner (unchanged)
├── strategy.py          ← Original RSI/MACD strategy (unchanged)
├── trader.py            ← Original trade handler (unchanged)
├── market_filter.py     ← Liquidity filter (unchanged)
├── universe.py          ← Universe loader (unchanged)
├── config.py            ← All configuration
└── utils.py             ← Helpers
```

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure API keys
```bash
cp .env.example .env
# Edit .env and add your keys
```

**Tradier:** Sign up at tradier.com → get API key + account ID
- Use sandbox=true for paper trading first
- Switch to sandbox=false only when you're confident

**Unusual Whales:** unusualwhales.com → API access in account settings

### 3. Paper trade first
```bash
# Make sure TRADIER_SANDBOX=true in your .env
python executor.py
```

### 4. Go live
```bash
# Change TRADIER_SANDBOX=false in your .env
python executor.py
```

## Risk Controls

| Control | Default | Where to change |
|---|---|---|
| Risk per trade | 2% of account | `risk_manager.py` → `RISK_PER_TRADE_PCT` |
| Daily loss limit | 5% of account | `risk_manager.py` → `DAILY_LOSS_LIMIT_PCT` |
| Max open positions | 5 | `risk_manager.py` → `MAX_OPEN_POSITIONS` |
| Take profit | +100% | `position_manager.py` → `TAKE_PROFIT_PCT` |
| Stop loss | -50% | `position_manager.py` → `STOP_LOSS_PCT` |
| Min setup score | 50/100 | `executor.py` → `MIN_SETUP_SCORE` |

## Scanner Filters

A setup must pass at least 2 of 4 filters (configurable in `options_scanner.py`):

1. **Earnings** — within 1-5 days
2. **Options Flow** — $50K+ in call sweeps (Unusual Whales)
3. **News** — bullish headline in last 48 hours
4. **Momentum** — score 40+/100 (volume surge, ROC, RSI, 52W high proximity)

## How the Bot Selects Options

- **Expiry:** First date at least 2 days AFTER earnings, within 5-45 DTE
- **Strike:** Targets delta 0.35-0.60 (near ATM for balanced leverage)
- **Liquidity:** Bid/ask spread under 15%, open interest 50+
- **Sizing:** 2% of account equity, capped at 5% per position

## Running the Scanner Only (no execution)
```bash
python options_scanner.py
```
Outputs a ranked CSV of setups — great for manual review or content creation.
