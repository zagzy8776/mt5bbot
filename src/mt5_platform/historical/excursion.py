"""MAE / MFE computation from the actual price path.

Maximum Adverse Excursion (MAE): how far the price moved against the trade
between entry and exit. Always non-negative, expressed in price units.

Maximum Favorable Excursion (MFE): how far the price moved in favor of the
trade between entry and exit. Always non-negative, expressed in price units.

These are computed from the full tick/candle path the system observed
during the position's lifetime. They are NOT estimated from entry/exit
alone — that would miss the path and lie about risk.
"""

from __future__ import annotations

from mt5_platform.common.enums import OrderSide
from mt5_platform.context import Candle


def compute_mae_mfe(
    direction: OrderSide,
    entry: float,
    *,
    candles: list[Candle] | None = None,
    prices: list[float] | None = None,
) -> tuple[float, float, float, float]:
    """Compute MAE, MFE, MAE%, MFE% from the observed price path.

    For a LONG trade:
      adverse = how far below entry
      favorable = how far above entry
    For a SHORT trade:
      adverse = how far above entry
      favorable = how far below entry

    Returns (mae, mfe, mae_pct, mfe_pct) all >= 0.
    """
    if entry <= 0:
        return 0.0, 0.0, 0.0, 0.0

    path: list[float] = []
    if prices is not None:
        path = list(prices)
    elif candles is not None:
        # Use the close of each candle as the path point. Using lows/highs
        # would double-count; a candle is one observation in the path.
        path = [c.close for c in candles]
    if not path:
        return 0.0, 0.0, 0.0, 0.0

    if direction is OrderSide.BUY:
        worst_adverse = min(path)  # lowest point
        best_favorable = max(path)  # highest point
        mae = max(0.0, entry - worst_adverse)
        mfe = max(0.0, best_favorable - entry)
    else:  # SELL
        worst_adverse = max(path)  # highest point
        best_favorable = min(path)  # lowest point
        mae = max(0.0, worst_adverse - entry)
        mfe = max(0.0, entry - best_favorable)

    mae_pct = (mae / entry) * 100.0
    mfe_pct = (mfe / entry) * 100.0
    return mae, mfe, mae_pct, mfe_pct
