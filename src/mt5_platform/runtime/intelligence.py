"""Intelligence layer wiring for the live runtime (optional, behind INTELLIGENCE_ENABLED).

When enabled, every cycle:
1. Feed the current tick to MarketContextEngine -> canonical MarketContext
2. Query HistoricalEngine for comparable-setup evidence
3. Run the agent network -> SynthesisAgent -> TradeThesis
4. If thesis action is BUY/SELL, emit a StrategySignal tagged with thesis metadata
5. Evaluate open positions via PositionManager -> close/reduce if thesis invalidated
6. Record completed-trade outcomes to DecisionMemory + PostTradeReviewEngine

Default: OFF. The basic flow is the proven production path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mt5_platform.agents import (
    AgentContext,
    Stance,
    TradeThesis,
    run_agent_network,
)
from mt5_platform.backtest.data import timeframe_minutes
from mt5_platform.common.events import (
    MarketDataEvent,
    PositionInfo,
    StrategySignal,
    utc_now,
)
from mt5_platform.context import MarketContext, MarketContextEngine
from mt5_platform.historical.engine import EvidenceEngine, HistoricalQuery
from mt5_platform.historical.ledger import InMemoryHistoricalLedger
from mt5_platform.historical.models import SetupFeatures
from mt5_platform.learning import (
    DecisionMemory,
    DecisionMemoryRecord,
    HypothesisRegistry,
    PostTradeReviewEngine,
)
from mt5_platform.position import PositionManager, PositionManagerConfig


@dataclass
class IntelligenceStats:
    contexts_built: int = 0
    theses_emitted: int = 0
    theses_no_trade: int = 0
    signals_from_thesis: int = 0
    position_evaluations: int = 0
    position_exits: int = 0
    learning_records: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


class IntelligenceLayer:
    """Wraps Context + Agents + Synthesis + Historical + PositionManager + Learning."""

    def __init__(self, *, symbol: str = "XAUUSD", timeframe: str = "M15") -> None:
        self.symbol = symbol.upper()
        self.timeframe = timeframe.upper()
        primary_s = timeframe_minutes(self.timeframe) * 60
        self.context_engine = MarketContextEngine(
            symbol=self.symbol,
            primary_timeframe_s=primary_s,
        )
        self.historical_ledger = InMemoryHistoricalLedger()
        self.historical_engine = EvidenceEngine(self.historical_ledger)
        self.position_manager = PositionManager(config=PositionManagerConfig())
        self.decision_memory = DecisionMemory()
        self.hypothesis_registry = HypothesisRegistry()
        self.review_engine = PostTradeReviewEngine()
        self.stats = IntelligenceStats()
        self._last_context: MarketContext | None = None
        self._last_thesis: TradeThesis | None = None

    def feed_tick(
        self, *, bid: float, ask: float, timestamp: datetime | None = None
    ) -> MarketContext | None:
        try:
            ts = timestamp or utc_now()
            event = MarketDataEvent(
                timestamp=ts, source="intelligence", symbol=self.symbol, bid=bid, ask=ask
            )
            self.context_engine.update(event)
            ctx = self.context_engine.build_context()
            if ctx is not None:
                self._last_context = ctx
                self.stats.contexts_built += 1
            return ctx
        except Exception:
            self.stats.errors += 1
            return self._last_context

    def produce_thesis(self, ctx: MarketContext) -> TradeThesis | None:
        try:
            agent_ctx = AgentContext(
                market_context=ctx,
                historical_evidence=self._query_historical_evidence(ctx),
            )
            thesis = run_agent_network(agent_ctx)
            self._last_thesis = thesis
            if thesis.action in (Stance.BUY, Stance.SELL):
                self.stats.theses_emitted += 1
            else:
                self.stats.theses_no_trade += 1
            return thesis
        except Exception:
            self.stats.errors += 1
            return None

    def thesis_to_signal(self, thesis: TradeThesis) -> StrategySignal | None:
        if thesis.action not in (Stance.BUY, Stance.SELL):
            return None
        if thesis.direction is None or thesis.entry is None or thesis.stop_loss is None:
            return None
        self.stats.signals_from_thesis += 1
        return StrategySignal(
            symbol=thesis.instrument,
            direction=thesis.direction,
            entry=thesis.entry,
            stop_loss=thesis.stop_loss,
            take_profit=thesis.take_profit,
            confidence=thesis.confidence,
            reason=f"thesis:{thesis.thesis_id}",
            strategy_name="intelligence_synthesis",
            metadata={
                "thesis_id": thesis.thesis_id,
                "context_id": thesis.context_id,
                "regime": thesis.regime,
                "aligned_agents": thesis.aligned_agents,
                "opposed_agents": thesis.opposed_agents,
                "reasons": thesis.reasons,
                "invalidation": thesis.invalidation,
            },
        )

    def evaluate_positions(
        self, positions: list[PositionInfo], ctx: MarketContext
    ) -> list[tuple[PositionInfo, Any]]:
        results: list[tuple[PositionInfo, Any]] = []
        now = utc_now()
        for pos in positions:
            try:
                from mt5_platform.position.models import PositionState, ThesisSnapshot

                # Preserve original thesis from the last emitted trade thesis, if
                # the position was opened by the intelligence layer.
                thesis_snap = None
                thesis_id = ""
                if self._last_thesis is not None and self._last_thesis.instrument == pos.symbol:
                    thesis_id = self._last_thesis.thesis_id
                    thesis_snap = ThesisSnapshot(
                        thesis_id=thesis_id,
                        instrument=pos.symbol,
                        direction=pos.side,
                        regime=self._last_thesis.regime,
                        confidence=self._last_thesis.confidence,
                        entry=pos.entry_price,
                        stop_loss=pos.stop_loss,
                        take_profit=pos.take_profit,
                        invalidation_levels=self._last_thesis.invalidation,
                        reasons=self._last_thesis.reasons,
                    )

                state = PositionState(
                    position_id=pos.ticket,
                    instrument=pos.symbol,
                    direction=pos.side,
                    volume=pos.volume,
                    entry_price=pos.entry_price,
                    current_price=pos.current_price,
                    unrealized_pnl=pos.floating_pnl,
                    stop_loss=pos.stop_loss,
                    take_profit=pos.take_profit,
                    opened_at=pos.opened_at,
                    last_update=now,
                    thesis_id=thesis_id,
                    thesis_snapshot=thesis_snap,
                )
                decision = self.position_manager.evaluate(state, ctx)
                results.append((pos, decision))
                self.stats.position_evaluations += 1
            except Exception:
                self.stats.errors += 1
        return results

    def record_completed_trade(
        self,
        *,
        signal: StrategySignal,
        entry: float,
        exit_price: float,
        volume: float,
        pnl: float,
    ) -> None:
        try:
            record = DecisionMemoryRecord(
                trade_id=f"trade_{signal.signal_id}",
                thesis_id=signal.metadata.get("thesis_id", ""),
                context_id=signal.metadata.get("context_id", ""),
                instrument=signal.symbol,
                direction=signal.direction.value,
                entry=entry,
                exit=exit_price,
                volume=volume,
                realized_pnl=pnl,
            )
            self.decision_memory.store(record)
            self.stats.learning_records += 1
        except Exception:
            self.stats.errors += 1

    def _query_historical_evidence(self, ctx: MarketContext) -> dict[str, Any] | None:
        try:
            if ctx.regime is None:
                return None
            setup = SetupFeatures(
                instrument=ctx.instrument,
                timestamp=ctx.timestamp or utc_now(),
                regime=ctx.regime,
                session=ctx.session.label if ctx.session else "",
            )
            query = HistoricalQuery(setup=setup, as_of=utc_now())
            report = self.historical_engine.query(query)
            return report.to_dict()
        except Exception:
            return None


__all__ = ["IntelligenceLayer", "IntelligenceStats"]