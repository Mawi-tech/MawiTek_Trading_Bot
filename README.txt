MawiTek Universe Rotation Patch
================================
Date: May 2026

WHAT THIS FIXES
---------------
The universe scanner was alphabetically slicing the first 100 S&P tickers
on every cycle, creating a permanent blind spot. Tickers like NVDA, MSFT,
PLTR, RBLX, HOOD were never scanned.

WHAT'S IN THIS PATCH
--------------------
1. universe.py
   - Adds "mode" parameter to load_universe() and get_default_universe()
   - Three modes: "rotate" (new default), "random", "head" (legacy)
   - Rotation persists across bot restarts via .universe_state.json
   - Includes reset_rotation_state() helper

2. bot.py
   - Moves build_tradable_universe() INSIDE the while loop
   - Without this change, rotation would freeze on whatever window
     startup happened to land on

3. options_bot_documentation.docx
   - Added universe.py to Core Infrastructure table (Section 2)
   - New "Universe Rotation" subsection explaining the three modes
   - Added .universe_state.json to runtime-files table (Section 13)
   - Date bumped to May 2026

HOW TO APPLY
------------
Drop all three files into:
  A:\Mawitek Trading Bot\MawiTek_Trading_Bot\MawiTek_Trading_Bot\

Overwrite existing files when prompted.

VERIFICATION
------------
After applying, start the bot. You should see log lines like:

  [Universe] Rotation window: offset=0 -> next=100 (scanning AAPL..XYZ)

Each cycle, the offset advances by `limit`. Once it wraps past the end
of the list, it continues from the start. With limit=100 against the
full S&P 500, full coverage takes ~25 minutes (5 cycles x 5min sleep).

To reset rotation back to position 0:
  - Delete .universe_state.json (created next to universe.py), OR
  - In Python: from universe import reset_rotation_state; reset_rotation_state()

REMAINING OPEN ITEMS (not in this patch)
----------------------------------------
- Anomalous V/OI ratios on freshly-listed weekly expiries
- News catalyst signal returning zero articles
- Missing files: iv_rank_bot.py, hft_executor.py, hft_scanner.py,
  backtest_hft.py, dashboard_server.py, sandbox_validator.py
