"""Account monitoring — unsafe conditions halt new trades."""

from __future__ import annotations

from mt5_platform.common.events import AccountSnapshot
from mt5_platform.config import Settings
from mt5_platform.risk import RiskEngine


class AccountMonitor:
    def __init__(self, settings: Settings, risk_engine: RiskEngine) -> None:
        self.settings = settings
        self.risk_engine = risk_engine
        self.last_snapshot: AccountSnapshot | None = None
        self.unsafe = False
        self.unsafe_reason: str | None = None

    def evaluate(self, snapshot: AccountSnapshot) -> bool:
        """Return True if account is safe for new trades."""
        self.last_snapshot = snapshot
        if snapshot.drawdown_pct >= self.settings.max_drawdown_pct:
            self._mark_unsafe("max_drawdown_exceeded")
            return False
        if snapshot.balance > 0:
            daily_loss_pct = max(0.0, -snapshot.daily_pnl / snapshot.balance * 100.0)
            if daily_loss_pct >= self.settings.max_daily_loss_pct:
                self._mark_unsafe("max_daily_loss")
                return False
        if snapshot.free_margin <= 0:
            self._mark_unsafe("no_free_margin")
            return False
        self.unsafe = False
        self.unsafe_reason = None
        return True

    def _mark_unsafe(self, reason: str) -> None:
        self.unsafe = True
        self.unsafe_reason = reason
        self.risk_engine.engage_kill_switch(reason)
