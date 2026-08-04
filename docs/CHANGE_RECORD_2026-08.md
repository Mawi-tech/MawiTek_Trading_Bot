# Change record — security hardening pass, July–August 2026

Every risk-relevant change landed on `main` between `60b2fb5` (2026-07-30, before this pass) and
`01a64c0` (2026-08-04). Eleven non-merge commits across five pull requests.

Scope note: "risk-relevant" here means anything that can change **whether a trade is placed, at what
size, or whether the bot halts** — not only literal constants. Most of this pass changed *behaviour*
without changing a number, so both categories are recorded.

---

## 1. Literal parameter changes

Exactly one constant changed value in this range.

| File | Constant | Old | New | Commit | PR | Merged |
|---|---|---|---|---|---|---|
| `executor.py` | `MIN_SETUP_SCORE` | `50` | `65` | `84e8a62` | #20 | 2026-08-04 |

**This was not part of the hardening work.** It was an uncommitted edit sitting in the working tree
when the pass began, and it was swept into commit `84e8a62` ("Fail closed when risk state or the
pending-order ledger is corrupt") because that commit needed an unrelated edit to the same file and
was staged with `git add executor.py`, which takes the whole file.

Effect: Strategy 1 (catalyst long calls) now requires a setup score of 65 rather than 50 before it
will trade. Directionally **risk-reducing** — fewer, higher-conviction entries — and the accompanying
comment cites the evidence (`50-59 bucket bled -$541/trade`). It is almost certainly intended. It is
recorded here because it reached `main` inside an unrelated security commit rather than on its own
merits, and because a parameter that governs entry frequency should never land unreviewed.

No other uncommitted work was affected. Verified file by file against `main`:
`position_manager.py`, `event_notifier.py`, `tests/test_exit_alerts.py` and `tests/test_setup_alerts.py`
are all unchanged on `main` and remain only in the working tree.

---

## 2. Behavioural changes that alter risk posture without changing a constant

These change what the existing limits actually *do*. Several were previously inert.

| Area | Change | Commit | PR | Merged |
|---|---|---|---|---|
| Per-strategy capital cap | `deployed_capital_by_strategy()` never read `iv_rank_positions.json`, so IV-Rank reported **$0 deployed permanently** and its 35% allocation ceiling **never bound**. Now reports `max_risk × qty` (collateral — the correct measure for a net-credit spread, where `entry_price × qty` is meaningless). Also closes the same blind spot for `bounce_positions.json` and `vwap_fade_positions.json`. **Risk-reducing:** IV-Rank now receives less budget and can be rejected with `iv_rank capital allocation full`. | `ef520ac` | #11 | 2026-07-30 |
| Daily-loss halt | `risk_manager.load_state()` caught every exception and returned a default of `halted=False, trades_today=0, realized_pnl=0.0`. A corrupt `risk_state.json` therefore **silently cleared an active halt** and resumed trading. Now reads strictly: the file is quarantined to `<path>.corrupt.<ts>` and `StateCorruption` is raised. | `84e8a62` | #20 | 2026-08-04 |
| Crash recovery | `order_manager._load_pending()` returned `{}` on a corrupt ledger, telling recovery **nothing was in flight** and orphaning any real broker fill with no local position and no exit management. Now reads strictly. | `84e8a62` | #20 | 2026-08-04 |
| Strategy startup | All six strategies wrapped `recover_pending_orders()` in `except Exception: … (non-fatal)`, which would have swallowed the above. Each now catches `StateCorruption` first and calls `abort_on_corruption` — ERROR log, `event_notifier` alert, `exit(1)`. | `84e8a62` | #20 | 2026-08-04 |
| Cross-process state integrity | `file_lock` had two races that let **two processes hold one lock**: TOCTOU stale-breaking, and unconditional release deleting a new holder's lock. Now uses a `<pid>:<uuid4>` per-acquisition token; release and stale-break are both conditional on it. Protects every shared state file including `risk_state.json`. | `0265a6e` | #20 | 2026-08-04 |
| Kill-switch exposure | `POST /api/control` (halt / flatten) accepted **unauthenticated cross-origin POSTs**. Now requires `application/json` (415), rejects cross-site `Sec-Fetch-Site` / mismatched `Origin` (403), and pins `Host` to loopback (421). | `63b950c` | #14 | 2026-08-04 |
| Kill-switch exposure | Binding beyond loopback with no `DASH_AUTH_USER` / `DASH_AUTH_PASS` now **exits 2** instead of starting an unauthenticated listener reachable from the LAN. | `c31ab22` | #20 | 2026-08-04 |
| Test-run safety | `MOCK_MODE` is derived from absent credentials. With a real `.env` present, the suite ran with `MOCK_MODE = False` — one un-monkeypatched call away from live broker traffic. `tests/conftest.py` now pins it on before the first bot import, matching CI. | `ce4ee63` | #13 | 2026-07-31 |

---

## 3. Non-risk changes in the same range, for completeness

| Area | Change | Commit | PR | Merged |
|---|---|---|---|---|
| Dashboard | `dashboard.html` split into `dashboard.css` + `dashboard.js` (pure extraction) | `ad30d24` | #11 | 2026-07-30 |
| Dashboard | Each `dashboard_state` builder isolated so one failure cannot blank the whole file | `c6d02fa` | #11 | 2026-07-30 |
| CI | `pytest` on 3.10/3.11/3.12, SHA-pinned actions, `requirements.txt` pinned, live badge replacing a static image | `1104d3b` | #12 | 2026-07-30 |
| Server | Served-file allowlist changed from extension-matching to exact filenames | `e3ba4d1` | #20 | 2026-08-04 |
| Server / state | `X-XSS-Protection: 0`; UTF-8 byte auth compare with no short-circuit; directory `fsync` after `os.replace`; `sweep_stale_temp_files()` at launch | `9826bd0` | #20 | 2026-08-04 |

---

## 4. Verification at `01a64c0`

- Full suite **673 passed** locally and on CI across Python 3.10 / 3.11 / 3.12.
- Presence of every hardening change confirmed by content inspection of `main`, not by commit subject.
- `_ALLOWED_EXTS` confirmed absent (0 occurrences); `X-XSS-Protection` confirmed `"0"`.

## 5. Open item

`MIN_SETUP_SCORE = 65` (section 1) reached `main` without its own review. Confirm it is intended.
To revert it in isolation:

```
git revert --no-commit 84e8a62 -- executor.py   # then re-apply only the StateCorruption hunks
```

Simpler in practice: edit the constant back to `50` in a one-line commit, since `84e8a62`'s other
change to `executor.py` (the `StateCorruption` import and handler) must stay.
