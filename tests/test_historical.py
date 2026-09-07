"""Phase C — Historical Evidence Engine tests.

Covers:
- MAE long / short
- MFE long / short
- Expectancy, win/loss, profit factor
- Regime / session / strategy / direction breakdowns
- Insufficient samples are explicitly marked
- Similar-context matching
- Historical Agent output
- Empty historical database
- Malformed historical records
- Future-data leakage
- Chronological filtering
- Deterministic results
- Walk-forward boundaries
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mt5_platform.agents import Stance as _Stance
from mt5_platform.common.enums import (
    DataQualityLevel,
    EvidenceQuality,
    OrderSide,
    RegimeLabel,
    TradeCause,
)
from mt5_platform.context import (
    BreakoutState,
    Candle,
    DataQuality,
    LiquidityState,
    MarketContext,
    SessionInfo,
    TrendFeatures,
    VolatilityFeatures,
)
from mt5_platform.historical import (
    EvidenceEngine,
    HistoricalQuery,
    InMemoryHistoricalLedger,
    SetupFeatures,
    SimilarityScorer,
    calculate_stats,
    calculate_streak,
    compute_mae_mfe,
)
from mt5_platform.historical.context_bridge import setup_from_context
from mt5_platform.historical.models import HistoricalOutcome

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(year: int = 2026, month: int = 1, day: int = 1, hour: int = 10) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _make_outcome(
    *,
    trade_id: str = "trade_1",
    instrument: str = "XAUUSD",
    strategy: str = "sma_crossover",
    direction: OrderSide = OrderSide.BUY,
    timestamp: datetime | None = None,
    entry: float = 100.0,
    stop_loss: float = 99.0,
    take_profit: float = 102.0,
    exit_price: float | None = 101.0,
    exit_time: datetime | None = None,
    exit_reason: TradeCause = TradeCause.TARGET_HIT,
    realized_pnl: float = 1.0,
    return_pct: float = 1.0,
    duration_s: float = 600.0,
    mae: float = 0.5,
    mfe: float = 1.5,
    mae_pct: float = 0.5,
    mfe_pct: float = 1.5,
    regime: RegimeLabel | None = RegimeLabel.TRENDING,
    session: str = "london",
    timeframe: str = "M5",
    trend_slope_pct: float | None = 0.3,
    trend_efficiency: float | None = 0.7,
    volatility_atr: float | None = 1.0,
    volatility_atr_to_median: float | None = 1.0,
    momentum_roc_pct: float | None = 0.5,
    momentum_persistence: float | None = 0.8,
    structure_trend: str = "up",
    range_position: float | None = 0.5,
    breakout_state: str = "none",
    data_quality: str = "ok",
    cause_class: TradeCause | None = None,
) -> HistoricalOutcome:
    ts = timestamp or _ts()
    features = SetupFeatures(
        instrument=instrument,
        timestamp=ts,
        regime=regime,
        session=session,
        timeframe=timeframe,
        trend_slope_pct=trend_slope_pct,
        trend_efficiency=trend_efficiency,
        volatility_atr=volatility_atr,
        volatility_atr_to_median=volatility_atr_to_median,
        momentum_roc_pct=momentum_roc_pct,
        momentum_persistence=momentum_persistence,
        structure_trend=structure_trend,
        range_position=range_position,
        breakout_state=breakout_state,
        data_quality=data_quality,
    )
    return HistoricalOutcome(
        trade_id=trade_id,
        instrument=instrument,
        strategy=strategy,
        direction=direction,
        timestamp=ts,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        exit_price=exit_price,
        exit_time=exit_time or (ts + timedelta(seconds=duration_s)),
        exit_reason=exit_reason,
        realized_pnl=realized_pnl,
        return_pct=return_pct,
        duration_s=duration_s,
        mae=mae,
        mfe=mfe,
        mae_pct=mae_pct,
        mfe_pct=mfe_pct,
        features=features,
        cause_class=cause_class or exit_reason,
    )


def _ctx(
    *,
    regime: RegimeLabel | None = RegimeLabel.TRENDING,
    session: str = "london",
    timestamp: datetime | None = None,
    current_price: float = 2550.0,
) -> MarketContext:
    ts = timestamp or _ts()
    candles = [
        Candle(
            timestamp=ts - timedelta(minutes=5 * (60 - i)),
            open=2549.0,
            high=2551.0,
            low=2548.0,
            close=2550.0 + i * 0.1,
            volume=1.0,
        )
        for i in range(60)
    ]
    return MarketContext(
        instrument="XAUUSD",
        timestamp=ts,
        current_price=current_price,
        session=SessionInfo(label=session, utc_hour=ts.hour, weekday="monday"),
        candles={"M5": candles},
        trend=TrendFeatures(slope_per_bar_pct=0.3, efficiency_ratio=0.7),
        volatility=VolatilityFeatures(atr=5.0, atr_to_median=1.0),
        breakout=BreakoutState(state="none"),
        liquidity=LiquidityState(level="normal"),
        regime=regime,
        regime_confidence=0.8,
        regime_evidence={},
        data_quality=DataQuality(level=DataQualityLevel.OK),
        usable_for_trading=True,
    )


# ===========================================================================
# MAE / MFE
# ===========================================================================


class TestMAEMFE:
    def test_mae_long(self) -> None:
        # LONG: price dips to 98, recovers. Entry 100, stop 99.
        mae, mfe, mae_pct, mfe_pct = compute_mae_mfe(
            OrderSide.BUY, 100.0, prices=[100, 99, 98, 99.5, 101, 102]
        )
        assert mae == pytest.approx(2.0)  # 100 - 98
        assert mfe == pytest.approx(2.0)  # 102 - 100
        assert mae_pct == pytest.approx(2.0)
        assert mfe_pct == pytest.approx(2.0)

    def test_mfe_long(self) -> None:
        # LONG: price spikes to 103, then drops to 99. Entry 100.
        mae, mfe, _, _ = compute_mae_mfe(OrderSide.BUY, 100.0, prices=[100, 103, 101, 99])
        assert mfe == pytest.approx(3.0)
        assert mae == pytest.approx(1.0)

    def test_mae_short(self) -> None:
        # SHORT: price spikes to 102, drops to 98. Entry 100.
        mae, mfe, _, _ = compute_mae_mfe(OrderSide.SELL, 100.0, prices=[100, 102, 101, 98, 99])
        assert mae == pytest.approx(2.0)  # 102 - 100
        assert mfe == pytest.approx(2.0)  # 100 - 98

    def test_mfe_short(self) -> None:
        # SHORT: price drops to 97, then spikes to 101. Entry 100.
        mae, mfe, _, _ = compute_mae_mfe(OrderSide.SELL, 100.0, prices=[100, 97, 99, 101, 98])
        assert mfe == pytest.approx(3.0)  # 100 - 97
        assert mae == pytest.approx(1.0)  # 101 - 100

    def test_mae_mfe_from_candles(self) -> None:
        candles = [
            Candle(timestamp=_ts(), open=100, high=103, low=98, close=100, volume=1.0)
            for _ in range(3)
        ]
        mae, mfe, _, _ = compute_mae_mfe(OrderSide.BUY, 100.0, candles=candles)
        # Closes are all 100, so MFE=0, MAE=0 from closes alone.
        assert mfe == 0.0
        assert mae == 0.0

    def test_empty_path_returns_zeros(self) -> None:
        mae, mfe, mae_pct, mfe_pct = compute_mae_mfe(OrderSide.BUY, 100.0)
        assert mae == 0.0 and mfe == 0.0

    def test_zero_entry_returns_zeros(self) -> None:
        mae, mfe, _, _ = compute_mae_mfe(OrderSide.BUY, 0.0, prices=[100, 101])
        assert mae == 0.0 and mfe == 0.0


# ===========================================================================
# Statistics
# ===========================================================================


class TestStatistics:
    def test_empty_outcomes(self) -> None:
        stats = calculate_stats([])
        assert stats.sample_size == 0
        assert stats.evidence_quality == EvidenceQuality.INSUFFICIENT

    def test_win_rate_and_expectancy(self) -> None:
        outcomes = [
            _make_outcome(trade_id=f"t{i}", return_pct=rp, realized_pnl=rp)
            for i, rp in enumerate([2.0, -1.0, 3.0, -1.5, 1.0, -0.5])
        ]
        stats = calculate_stats(outcomes, min_strong=100, min_moderate=5, min_weak=3)
        assert stats.sample_size == 6
        assert stats.wins == 3
        assert stats.losses == 3
        assert stats.win_rate == pytest.approx(0.5)
        assert stats.expectancy == pytest.approx((2.0 - 1.0 + 3.0 - 1.5 + 1.0 - 0.5) / 6)
        assert stats.evidence_quality == EvidenceQuality.MODERATE

    def test_profit_factor(self) -> None:
        outcomes = [
            _make_outcome(trade_id="a", return_pct=3.0, realized_pnl=3.0),
            _make_outcome(trade_id="b", return_pct=-1.0, realized_pnl=-1.0),
        ]
        stats = calculate_stats(outcomes, min_strong=100, min_moderate=30, min_weak=10)
        assert stats.profit_factor == pytest.approx(3.0)

    def test_all_wins_profit_factor_capped(self) -> None:
        # No losses at all — profit_factor should be finite, not infinity.
        outcomes = [
            _make_outcome(trade_id=f"w{i}", return_pct=1.0, realized_pnl=1.0) for i in range(5)
        ]
        stats = calculate_stats(outcomes, min_strong=100, min_moderate=30, min_weak=10)
        assert stats.profit_factor == 5.0  # capped to sample size
        assert stats.losses == 0

    def test_avg_mae_mfe(self) -> None:
        outcomes = [
            _make_outcome(trade_id="a", mae_pct=1.0, mfe_pct=2.0),
            _make_outcome(trade_id="b", mae_pct=3.0, mfe_pct=4.0),
        ]
        stats = calculate_stats(outcomes, min_strong=100, min_moderate=30, min_weak=10)
        assert stats.avg_mae_pct == pytest.approx(2.0)
        assert stats.avg_mfe_pct == pytest.approx(3.0)

    def test_outcome_distribution(self) -> None:
        outcomes = [
            _make_outcome(
                trade_id="a", exit_reason=TradeCause.TARGET_HIT, cause_class=TradeCause.TARGET_HIT
            ),
            _make_outcome(
                trade_id="b", exit_reason=TradeCause.STOP_HIT, cause_class=TradeCause.STOP_HIT
            ),
            _make_outcome(
                trade_id="c", exit_reason=TradeCause.STOP_HIT, cause_class=TradeCause.STOP_HIT
            ),
        ]
        stats = calculate_stats(outcomes, min_strong=100, min_moderate=30, min_weak=10)
        assert stats.outcome_distribution == {"target_hit": 1, "stop_hit": 2}

    def test_max_losing_streak(self) -> None:
        outcomes = [
            _make_outcome(trade_id="a", realized_pnl=1.0, return_pct=1.0, exit_time=_ts(hour=1)),
            _make_outcome(trade_id="b", realized_pnl=-1.0, return_pct=-1.0, exit_time=_ts(hour=2)),
            _make_outcome(trade_id="c", realized_pnl=-1.0, return_pct=-1.0, exit_time=_ts(hour=3)),
            _make_outcome(trade_id="d", realized_pnl=-1.0, return_pct=-1.0, exit_time=_ts(hour=4)),
            _make_outcome(trade_id="e", realized_pnl=1.0, return_pct=1.0, exit_time=_ts(hour=5)),
            _make_outcome(trade_id="f", realized_pnl=-1.0, return_pct=-1.0, exit_time=_ts(hour=6)),
        ]
        streak = calculate_streak(outcomes)
        assert streak == 3

    def test_evidence_quality_thresholds(self) -> None:
        outcomes = [
            _make_outcome(trade_id=f"t{i}", return_pct=1.0, realized_pnl=1.0) for i in range(4)
        ]
        stats = calculate_stats(outcomes, min_strong=100, min_moderate=30, min_weak=10)
        assert stats.evidence_quality == EvidenceQuality.INSUFFICIENT

        outcomes += [_make_outcome(trade_id="t5", return_pct=1.0, realized_pnl=1.0)] * 6
        stats = calculate_stats(outcomes, min_strong=100, min_moderate=30, min_weak=10)
        assert stats.evidence_quality == EvidenceQuality.WEAK

    def test_open_outcomes_excluded(self) -> None:
        outcomes = [
            _make_outcome(trade_id="a", return_pct=1.0, realized_pnl=1.0, exit_price=101.0),
            _make_outcome(
                trade_id="b", return_pct=0.0, realized_pnl=0.0, exit_price=None
            ),  # still open
        ]
        stats = calculate_stats(outcomes, min_strong=100, min_moderate=30, min_weak=10)
        assert stats.sample_size == 1  # only the closed one counts


# ===========================================================================
# Similarity engine
# ===========================================================================


class TestSimilarity:
    def test_identical_setups_score_one(self) -> None:
        ts = _ts()
        f1 = SetupFeatures(
            instrument="XAUUSD",
            timestamp=ts,
            regime=RegimeLabel.TRENDING,
            session="london",
            trend_slope_pct=0.3,
            volatility_atr=1.0,
            momentum_roc_pct=0.5,
            structure_trend="up",
            range_position=0.5,
        )
        f2 = f1.model_copy(deep=True)
        score = SimilarityScorer().score(f1, f2)
        assert score == pytest.approx(1.0)

    def test_different_regime_reduces_similarity(self) -> None:
        ts = _ts()
        f1 = SetupFeatures(
            instrument="XAUUSD",
            timestamp=ts,
            regime=RegimeLabel.TRENDING,
            session="london",
        )
        f2 = SetupFeatures(
            instrument="XAUUSD",
            timestamp=ts,
            regime=RegimeLabel.RANGING,
            session="london",
        )
        score = SimilarityScorer().score(f1, f2)
        assert 0.0 <= score < 1.0

    def test_missing_features_treated_as_neutral(self) -> None:
        ts = _ts()
        f1 = SetupFeatures(instrument="XAUUSD", timestamp=ts, regime=RegimeLabel.TRENDING)
        f2 = SetupFeatures(instrument="XAUUSD", timestamp=ts, regime=RegimeLabel.TRENDING)
        # No comparable features except regime match
        score = SimilarityScorer().score(f1, f2)
        # Regime matches, everything else is neutral (0.5)
        assert 0.5 <= score <= 1.0

    def test_different_instruments_excluded(self) -> None:
        ts = _ts()
        ledger = InMemoryHistoricalLedger()
        ledger.record(_make_outcome(trade_id="t1", instrument="EURUSD", timestamp=ts))
        query_setup = SetupFeatures(
            instrument="XAUUSD",
            timestamp=ts + timedelta(days=1),
            regime=RegimeLabel.TRENDING,
        )
        engine = EvidenceEngine(ledger)
        report = engine.query(HistoricalQuery(setup=query_setup, as_of=_ts(day=2)))
        assert report.similar == []  # EURUSD excluded


# ===========================================================================
# Evidence engine
# ===========================================================================


class TestEvidenceEngine:
    def test_empty_ledger_returns_insufficient(self) -> None:
        engine = EvidenceEngine(InMemoryHistoricalLedger())
        setup = SetupFeatures(
            instrument="XAUUSD",
            timestamp=_ts(),
            regime=RegimeLabel.TRENDING,
        )
        report = engine.query(HistoricalQuery(setup=setup, as_of=_ts(day=2)))
        assert report.overall.sample_size == 0
        assert report.overall.evidence_quality == EvidenceQuality.INSUFFICIENT
        assert any("INSUFFICIENT" in n for n in report.notes)

    def test_overall_statistics(self) -> None:
        ledger = InMemoryHistoricalLedger()
        ts = _ts()
        for i, rp in enumerate([2.0, -1.0, 3.0, -0.5, 1.5, -2.0, 1.0, -1.0, 2.0, -0.5]):
            ledger.record(
                _make_outcome(
                    trade_id=f"t{i}",
                    return_pct=rp,
                    realized_pnl=rp,
                    timestamp=ts + timedelta(hours=i),
                )
            )
        engine = EvidenceEngine(ledger)
        setup = SetupFeatures(
            instrument="XAUUSD",
            timestamp=ts,
            regime=RegimeLabel.TRENDING,
        )
        report = engine.query(HistoricalQuery(setup=setup, as_of=ts + timedelta(hours=20)))
        assert report.overall.sample_size == 10
        assert report.overall.evidence_quality in (EvidenceQuality.WEAK, EvidenceQuality.MODERATE)

    def test_regime_breakdown(self) -> None:
        ledger = InMemoryHistoricalLedger()
        ts = _ts()
        for i in range(5):
            ledger.record(
                _make_outcome(
                    trade_id=f"t{i}",
                    timestamp=ts + timedelta(hours=i),
                    regime=RegimeLabel.TRENDING,
                )
            )
        for i in range(3):
            ledger.record(
                _make_outcome(
                    trade_id=f"r{i}",
                    timestamp=ts + timedelta(hours=10 + i),
                    regime=RegimeLabel.RANGING,
                )
            )
        engine = EvidenceEngine(ledger)
        setup = SetupFeatures(
            instrument="XAUUSD",
            timestamp=ts,
            regime=RegimeLabel.TRENDING,
        )
        report = engine.query(HistoricalQuery(setup=setup, as_of=ts + timedelta(days=2)))
        assert "trending" in report.by_regime
        assert "ranging" not in report.by_regime  # only query regime
        assert report.by_regime["trending"].sample_size == 5

    def test_session_breakdown(self) -> None:
        ledger = InMemoryHistoricalLedger()
        ts = _ts()
        for i in range(4):
            ledger.record(
                _make_outcome(
                    trade_id=f"l{i}",
                    timestamp=ts + timedelta(hours=i),
                    session="london",
                )
            )
        for i in range(2):
            ledger.record(
                _make_outcome(
                    trade_id=f"n{i}",
                    timestamp=ts + timedelta(hours=10 + i),
                    session="new_york",
                )
            )
        engine = EvidenceEngine(ledger)
        setup = SetupFeatures(
            instrument="XAUUSD",
            timestamp=ts,
            session="london",
        )
        report = engine.query(HistoricalQuery(setup=setup, as_of=ts + timedelta(days=2)))
        assert report.by_session["london"].sample_size == 4
        assert "new_york" not in report.by_session

    def test_strategy_breakdown(self) -> None:
        ledger = InMemoryHistoricalLedger()
        ts = _ts()
        for i in range(3):
            ledger.record(
                _make_outcome(
                    trade_id=f"t{i}",
                    timestamp=ts + timedelta(hours=i),
                    strategy="sma_crossover",
                )
            )
        engine = EvidenceEngine(ledger)
        setup = SetupFeatures(instrument="XAUUSD", timestamp=ts)
        report = engine.query(
            HistoricalQuery(setup=setup, as_of=ts + timedelta(days=2), strategy="sma_crossover")
        )
        assert "sma_crossover" in report.by_strategy

    def test_direction_breakdown(self) -> None:
        ledger = InMemoryHistoricalLedger()
        ts = _ts()
        for i in range(3):
            ledger.record(
                _make_outcome(
                    trade_id=f"b{i}",
                    timestamp=ts + timedelta(hours=i),
                    direction=OrderSide.BUY,
                )
            )
        for i in range(2):
            ledger.record(
                _make_outcome(
                    trade_id=f"s{i}",
                    timestamp=ts + timedelta(hours=10 + i),
                    direction=OrderSide.SELL,
                )
            )
        engine = EvidenceEngine(ledger)
        setup = SetupFeatures(instrument="XAUUSD", timestamp=ts)
        report = engine.query(HistoricalQuery(setup=setup, as_of=ts + timedelta(days=2)))
        assert report.by_direction["buy"].sample_size == 3
        assert report.by_direction["sell"].sample_size == 2


# ===========================================================================
# Future-data leakage
# ===========================================================================


class TestFutureDataLeakage:
    def test_future_outcomes_excluded_by_as_of(self) -> None:
        ledger = InMemoryHistoricalLedger()
        ts = _ts()
        for i in range(3):
            ledger.record(
                _make_outcome(
                    trade_id=f"past{i}",
                    timestamp=ts + timedelta(hours=i),
                )
            )
        for i in range(3):
            ledger.record(
                _make_outcome(
                    trade_id=f"future{i}",
                    timestamp=ts + timedelta(days=10 + i),
                )
            )
        engine = EvidenceEngine(ledger)
        setup = SetupFeatures(
            instrument="XAUUSD",
            timestamp=ts,
            regime=RegimeLabel.TRENDING,
        )
        report = engine.query(HistoricalQuery(setup=setup, as_of=ts + timedelta(days=2)))
        # Only the 3 past outcomes should be visible
        assert report.overall.sample_size == 3

    def test_decision_unchanged_when_future_appended(self) -> None:
        """The same historical decision must remain unchanged when future
        observations are appended to the database."""
        ledger = InMemoryHistoricalLedger()
        ts = _ts()
        # Seed 10 outcomes at known times
        for i in range(10):
            ledger.record(
                _make_outcome(
                    trade_id=f"seed{i}",
                    timestamp=ts + timedelta(hours=i),
                    return_pct=1.0,
                    realized_pnl=1.0,
                )
            )
        engine = EvidenceEngine(ledger)
        setup = SetupFeatures(
            instrument="XAUUSD",
            timestamp=ts,
            regime=RegimeLabel.TRENDING,
        )
        query = HistoricalQuery(setup=setup, as_of=ts + timedelta(hours=20))
        before = engine.query(query)

        # Append future outcomes AFTER the as_of wall
        for i in range(5):
            ledger.record(
                _make_outcome(
                    trade_id=f"future{i}",
                    timestamp=ts + timedelta(days=30 + i),
                    return_pct=-10.0,
                    realized_pnl=-10.0,  # catastrophic
                )
            )
        after = engine.query(query)

        assert before.overall.sample_size == after.overall.sample_size
        assert before.overall.expectancy == after.overall.expectancy
        assert before.overall.win_rate == after.overall.win_rate
        assert before.overall.evidence_quality == after.overall.evidence_quality

    def test_walk_forward_window(self) -> None:
        """Walk-forward: a query at T2 with training_window excludes
        outcomes after T2 even if they exist in the ledger."""
        ledger = InMemoryHistoricalLedger()
        ts = _ts()
        for i in range(20):
            ledger.record(
                _make_outcome(
                    trade_id=f"t{i}",
                    timestamp=ts + timedelta(days=i),
                )
            )
        engine = EvidenceEngine(ledger)
        setup = SetupFeatures(instrument="XAUUSD", timestamp=ts)
        # Query at day 9.5 — strictly before day 10
        r1 = engine.query(HistoricalQuery(setup=setup, as_of=ts + timedelta(days=9, hours=12)))
        # Query at day 15.5 — includes day 15
        r2 = engine.query(HistoricalQuery(setup=setup, as_of=ts + timedelta(days=15, hours=12)))
        assert r1.overall.sample_size == 10  # days 0..9
        assert r2.overall.sample_size == 16  # days 0..15


# ===========================================================================
# Historical Agent integration
# ===========================================================================


class TestHistoricalAgentIntegration:
    def test_agent_with_evidence_engine(self) -> None:
        from mt5_platform.agents import AgentContext, HistoricalAgent

        ledger = InMemoryHistoricalLedger()
        ts = _ts()
        for i in range(50):
            ledger.record(
                _make_outcome(
                    trade_id=f"t{i}",
                    timestamp=ts + timedelta(hours=i),
                    return_pct=0.5,
                    realized_pnl=0.5,
                    regime=RegimeLabel.TRENDING,
                )
            )
        engine = EvidenceEngine(ledger)
        setup = SetupFeatures(
            instrument="XAUUSD",
            timestamp=ts,
            regime=RegimeLabel.TRENDING,
        )
        report = engine.query(HistoricalQuery(setup=setup, as_of=ts + timedelta(days=3)))
        evidence_dict = report.to_dict()["overall"]

        agent_ctx = AgentContext(
            market_context=_ctx(),
            historical_evidence=evidence_dict,
        )
        op = HistoricalAgent().evaluate(agent_ctx)
        assert op.stance in (StanceEnum_NEUTRAL, StanceEnum_CAUTION)

    def test_agent_with_insufficient_evidence(self) -> None:
        from mt5_platform.agents import AgentContext, HistoricalAgent

        agent_ctx = AgentContext(
            market_context=_ctx(),
            historical_evidence=None,
        )
        op = HistoricalAgent().evaluate(agent_ctx)
        assert op.stance is StanceEnum_CAUTION
        assert "no" in op.rationale.lower() or "insufficient" in op.rationale.lower()

    def test_agent_never_claims_directional_signal_directly(self) -> None:
        """Historical evidence must NOT become an automatic trading signal.
        Even with strong positive expectancy, the agent speaks NEUTRAL or
        CAUTION — never BUY/SELL — because direction is synthesis's job."""
        from mt5_platform.agents import AgentContext, HistoricalAgent

        agent_ctx = AgentContext(
            market_context=_ctx(),
            historical_evidence={
                "sample_size": 200,
                "evidence_quality": "strong",
                "expectancy": 5.0,
                "win_rate": 0.8,
            },
        )
        op = HistoricalAgent().evaluate(agent_ctx)
        assert op.stance is not StanceEnum_BUY
        assert op.stance is not StanceEnum_SELL


# ===========================================================================
# Context bridge
# ===========================================================================


class TestContextBridge:
    def test_setup_from_context(self) -> None:
        ctx = _ctx()
        setup = setup_from_context(ctx)
        assert setup.instrument == "XAUUSD"
        assert setup.regime == RegimeLabel.TRENDING
        assert setup.session == "london"
        assert setup.trend_slope_pct == pytest.approx(0.3)
        assert 0.0 <= (setup.range_position or 0) <= 1.0

    def test_setup_from_context_handles_missing_features(self) -> None:
        ctx = _ctx()
        ctx = ctx.model_copy(update={"trend": None, "volatility": None})
        setup = setup_from_context(ctx)
        assert setup.trend_slope_pct is None
        assert setup.volatility_atr is None


# ===========================================================================
# Determinism
# ===========================================================================


class TestDeterminism:
    def test_similarity_is_deterministic(self) -> None:
        ts = _ts()
        f1 = SetupFeatures(
            instrument="XAUUSD",
            timestamp=ts,
            regime=RegimeLabel.TRENDING,
            trend_slope_pct=0.3,
            momentum_roc_pct=0.5,
        )
        f2 = SetupFeatures(
            instrument="XAUUSD",
            timestamp=ts,
            regime=RegimeLabel.TRENDING,
            trend_slope_pct=0.4,
            momentum_roc_pct=0.4,
        )
        s1 = SimilarityScorer()
        score1 = s1.score(f1, f2)
        score2 = s1.score(f1, f2)
        assert score1 == score2

    def test_engine_is_deterministic(self) -> None:
        ledger = InMemoryHistoricalLedger()
        ts = _ts()
        for i in range(10):
            ledger.record(
                _make_outcome(
                    trade_id=f"t{i}",
                    timestamp=ts + timedelta(hours=i),
                    return_pct=0.5,
                    realized_pnl=0.5,
                )
            )
        engine = EvidenceEngine(ledger)
        setup = SetupFeatures(instrument="XAUUSD", timestamp=ts)
        q = HistoricalQuery(setup=setup, as_of=ts + timedelta(days=2))
        r1 = engine.query(q)
        r2 = engine.query(q)
        assert r1.overall.expectancy == r2.overall.expectancy
        assert r1.overall.win_rate == r2.overall.win_rate


# ---------------------------------------------------------------------------
# Stance enum alias for agent integration tests
# ---------------------------------------------------------------------------

StanceEnum_BUY = _Stance.BUY
StanceEnum_SELL = _Stance.SELL
StanceEnum_NEUTRAL = _Stance.NEUTRAL
StanceEnum_CAUTION = _Stance.CAUTION
