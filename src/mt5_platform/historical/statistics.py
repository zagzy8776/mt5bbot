"""Historical outcome statistics.

All calculations are deterministic over a finite list of HistoricalOutcome.
Every aggregate carries an explicit EvidenceQuality grade based on sample
size. The system must never present a 4-trade sample as strong evidence.
"""

from __future__ import annotations

from mt5_platform.common.enums import EvidenceQuality
from mt5_platform.historical.models import HistoricalOutcome, OutcomeStats


def _grade_quality(
    n: int,
    *,
    min_strong: int = 100,
    min_moderate: int = 30,
    min_weak: int = 10,
) -> EvidenceQuality:
    if n >= min_strong:
        return EvidenceQuality.STRONG
    if n >= min_moderate:
        return EvidenceQuality.MODERATE
    if n >= min_weak:
        return EvidenceQuality.WEAK
    return EvidenceQuality.INSUFFICIENT


def calculate_stats(
    outcomes: list[HistoricalOutcome],
    *,
    min_strong: int = 100,
    min_moderate: int = 30,
    min_weak: int = 10,
) -> OutcomeStats:
    """Compute aggregate statistics over a list of closed outcomes.

    Open outcomes (exit_price is None) are excluded — you cannot compute
    realized statistics on a trade that is still running.
    """
    closed = [o for o in outcomes if o.is_closed]
    n = len(closed)
    quality = _grade_quality(
        n, min_strong=min_strong, min_moderate=min_moderate, min_weak=min_weak
    )

    if n == 0:
        return OutcomeStats(
            sample_size=0,
            evidence_quality=EvidenceQuality.INSUFFICIENT,
            min_sample_strong=min_strong,
            min_sample_moderate=min_moderate,
            min_sample_weak=min_weak,
        )

    wins = [o for o in closed if o.is_winner]
    losses = [o for o in closed if o.is_loser]
    breakeven = [o for o in closed if o.realized_pnl == 0]
    win_n, loss_n, be_n = len(wins), len(losses), len(breakeven)

    returns = [o.return_pct for o in closed]
    avg_return = sum(returns) / n

    win_returns = [w.return_pct for w in wins] if wins else [0.0]
    loss_returns = [l.return_pct for l in losses] if losses else [0.0]
    avg_win = sum(win_returns) / len(win_returns) if win_returns else 0.0
    avg_loss = sum(loss_returns) / len(loss_returns) if loss_returns else 0.0

    # Profit factor: sum of gains / absolute sum of losses. 0 if no losses.
    gross_profit = sum(w.return_pct for w in wins)
    gross_loss = abs(sum(l.return_pct for l in losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        # No losses at all — strong but unprovable. Cap to avoid divide-by-zero illusion.
        profit_factor = float(n)
    else:
        profit_factor = 0.0

    avg_mae = sum(o.mae_pct for o in closed) / n
    avg_mfe = sum(o.mfe_pct for o in closed) / n

    # Outcome distribution by cause_class
    distribution: dict[str, int] = {}
    for o in closed:
        key = o.cause_class.value
        distribution[key] = distribution.get(key, 0) + 1

    return OutcomeStats(
        sample_size=n,
        wins=win_n,
        losses=loss_n,
        breakeven=be_n,
        win_rate=win_n / n,
        loss_rate=loss_n / n,
        expectancy=avg_return,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        avg_mae_pct=avg_mae,
        avg_mfe_pct=avg_mfe,
        max_losing_streak=calculate_streak(closed),
        outcome_distribution=distribution,
        evidence_quality=quality,
        min_sample_strong=min_strong,
        min_sample_moderate=min_moderate,
        min_sample_weak=min_weak,
    )


def calculate_streak(closed: list[HistoricalOutcome]) -> int:
    """Longest run of consecutive losses in chronological order.

    Input must already be sorted by exit time (or entry time — they are
    monotonic in practice). Returns 0 if no losses.
    """
    # Sort by exit_time, falling back to entry timestamp
    ordered = sorted(
        closed,
        key=lambda o: o.exit_time or o.timestamp,
    )
    max_streak = 0
    current = 0
    for o in ordered:
        if o.is_loser:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak
