"""
exit_manager.py — shared trailing-stop + scale-out exit logic.

The long-option strategies (hft / pead / bounce) all exited on a FIXED take-profit
/ stop-loss / time-stop. That leaves money on the table two ways:
  • a winner that runs to +90% then collapses back to the stop gives it all back,
  • an all-or-nothing exit never banks partial profit on the way up.

This module adds two behaviors on top of the existing TP/SL, as PURE decision
helpers so they're identical across strategies and unit-testable without a broker:

  • TRAILING STOP — once a position is up past `trail_activate`, track its peak
    P&L and exit if it gives back `trail_giveback` from that peak. Locks in a
    moving floor under a winner instead of riding it back to the hard stop.

  • SCALE-OUT — at a first target (`scale_trigger`), close a `scale_fraction` of
    the contracts ONCE, then let the rest run to the full TP / trailing stop.
    Banks profit and converts the remainder into a "house-money" runner.

All P&L values are FRACTIONS (0.50 = +50%). Config is per-strategy (the convex
0-DTE HFT book trails looser than the slower swing books).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TrailScaleConfig:
    trail_activate: float   # start trailing once P&L ≥ this (fraction)
    trail_giveback: float   # exit if P&L falls this far below the peak (fraction)
    scale_trigger:  float   # take partial profit once P&L ≥ this (fraction)
    scale_fraction: float   # portion of contracts to close at the scale-out
    scale_min_qty:  int = 2 # need at least this many contracts to scale (can't split 1)


# Per-strategy defaults. HFT is the convex intraday book — trail tighter and bank
# sooner; the swing books give winners more room.
HFT_EXIT    = TrailScaleConfig(trail_activate=0.40, trail_giveback=0.25, scale_trigger=0.50, scale_fraction=0.5)
PEAD_EXIT   = TrailScaleConfig(trail_activate=0.40, trail_giveback=0.25, scale_trigger=0.50, scale_fraction=0.5)
BOUNCE_EXIT = TrailScaleConfig(trail_activate=0.30, trail_giveback=0.20, scale_trigger=0.40, scale_fraction=0.5)


def update_peak(pos: dict, pnl_frac: float) -> float:
    """Raise the position's high-water-mark P&L; return the (new) peak. Mutates pos."""
    peak = max(float(pos.get("peak_pnl_pct", 0.0) or 0.0), pnl_frac)
    pos["peak_pnl_pct"] = peak
    return peak


def trailing_stop_hit(pnl_frac: float, peak_frac: float, cfg: TrailScaleConfig) -> bool:
    """
    True if the trailing stop should fire: the position got at least `trail_activate`
    in profit (so the trail is armed) and has since retraced `trail_giveback` from
    its peak. Never fires while underwater or below the activation threshold.
    """
    if peak_frac < cfg.trail_activate:
        return False
    return pnl_frac <= peak_frac - cfg.trail_giveback


def scale_out_quantity(pos: dict, pnl_frac: float, cfg: TrailScaleConfig) -> int:
    """
    Number of contracts to close as a partial scale-out, or 0.

    Fires ONCE (guarded by pos['scaled_out']) when P&L first reaches
    `scale_trigger`, and only if the position has enough contracts to split. The
    remainder keeps running on the full TP / trailing stop.
    """
    if pos.get("scaled_out"):
        return 0
    qty = int(pos.get("quantity", 0) or 0)
    if qty < cfg.scale_min_qty or pnl_frac < cfg.scale_trigger:
        return 0
    close_qty = int(qty * cfg.scale_fraction)
    return max(1, min(close_qty, qty - 1))   # keep at least 1 contract running
