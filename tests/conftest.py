"""Pytest config — put the project root on sys.path so tests can import the
bot modules directly (state_io, order_manager, ...) regardless of cwd, and pin
the broker into MOCK_MODE before any bot module is imported.

WHY MOCK_MODE IS FORCED HERE
----------------------------
`tradier_client` derives mock mode at import time:

    MOCK_MODE = (not TRADIER_API_KEY) or (not TRADIER_ACCOUNT_ID)

On a fresh checkout there is no `.env`, so MOCK_MODE came out True and the suite
ran offline as documented. On a developer machine with real credentials in
`.env`, it came out **False** — the suite ran with live broker calls one
un-monkeypatched call away, and `tests/test_market_clock.py` failed because it
asserts the MOCK_MODE branch that was no longer active.

Setting these to the empty string *before* the first bot import fixes both.
`load_dotenv(..., override=False)` skips any key already present in the
environment, so an empty value here wins over `.env`. This is exactly what
`.github/workflows/tests.yml` does, so a local run now matches CI.

Do not replace the empty strings with dummy values: non-empty credentials
switch MOCK_MODE *off*, which is the opposite of the intent.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Must precede any `import tradier_client` from a test module.
os.environ["TRADIER_API_KEY"] = ""
os.environ["TRADIER_ACCOUNT_ID"] = ""
os.environ.setdefault("TRADIER_SANDBOX", "true")
