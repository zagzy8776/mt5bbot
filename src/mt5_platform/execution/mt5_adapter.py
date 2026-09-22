"""MetaTrader 5 execution adapter.

Runs only where the MT5 terminal runs (the ``MetaTrader5`` package is Windows-only).
The MT5 module is injectable so the whole adapter is testable with a fake terminal.

Safety rules enforced here (defense in depth — the risk engine checks first):

* Real-money accounts are refused unless live mode is fully acknowledged in Settings.
* Every order must carry a stop loss, and the SL/TP are sent WITH the order so the broker
  holds them even if this process dies.
* Orders are tagged (magic number + comment) so a crash/retry can never open a duplicate,
  and reconciliation can find them again.
* Volume, stop distance and stop side are validated against the broker's own symbol rules
  before anything is sent; ``order_check`` runs before ``order_send``.
* All terminal calls run in a worker thread and are serialized (the MT5 API is blocking
  and not safe to call concurrently).
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from typing import Any

from mt5_platform.common.audit import audit_log
from mt5_platform.common.enums import AuditEventType, OrderSide, OrderStatus, Severity
from mt5_platform.common.events import (
    AccountSnapshot,
    AuditEvent,
    ExecutionRecord,
    OrderRequest,
    PositionInfo,
)
from mt5_platform.common.ids import new_execution_id
from mt5_platform.common.instruments import InstrumentSpec
from mt5_platform.config import Settings
from mt5_platform.execution.base import ExecutionAdapter


class RealAccountBlocked(RuntimeError):
    """Connected account is real-money but live mode is not fully acknowledged."""


class MT5NotAvailable(RuntimeError):
    """The MetaTrader5 package/terminal is unavailable on this machine."""


# The broker's own words when the terminal refuses to transmit: retcode 10027
# (TRADE_RETCODE_CLIENT_DISABLES_AT) with this comment.
TERMINAL_BLOCK_COMMENT = "AutoTrading disabled by client"


# Broker outcomes that are definitively "not filled" (safe to mark BROKER_REJECTED).
# Anything else that is not DONE/DONE_PARTIAL is treated as UNCERTAIN and raised, so the
# order manager marks the order FAILED and reconciliation resolves it against broker truth.
_DEFINITE_REJECTS = {
    "TRADE_RETCODE_REQUOTE",
    "TRADE_RETCODE_REJECT",
    "TRADE_RETCODE_CANCEL",
    "TRADE_RETCODE_INVALID",
    "TRADE_RETCODE_INVALID_VOLUME",
    "TRADE_RETCODE_INVALID_PRICE",
    "TRADE_RETCODE_INVALID_STOPS",
    "TRADE_RETCODE_TRADE_DISABLED",
    "TRADE_RETCODE_MARKET_CLOSED",
    "TRADE_RETCODE_NO_MONEY",
    "TRADE_RETCODE_PRICE_CHANGED",
    "TRADE_RETCODE_PRICE_OFF",
    "TRADE_RETCODE_INVALID_FILL",
    "TRADE_RETCODE_CLIENT_DISABLES_AT",
    "TRADE_RETCODE_LIMIT_VOLUME",
    "TRADE_RETCODE_LIMIT_ORDERS",
    "TRADE_RETCODE_POSITION_CLOSED",
}


class MT5ExecutionAdapter(ExecutionAdapter):
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client
        self._connected = False
        self._peak_equity = 0.0
        self._call_lock = asyncio.Lock()
        self._trade_lock = asyncio.Lock()
        self.executions: list[ExecutionRecord] = []
        # Last known terminal availability + the (single) block reason while it is unavailable.
        self._execution_availability: dict[str, Any] = {}
        self._submit_block: dict[str, Any] | None = None

    # ------------------------------------------------------------------ plumbing

    def _mt5(self) -> Any:
        if self._client is None:
            try:
                self._client = importlib.import_module("MetaTrader5")
            except ImportError as exc:
                raise MT5NotAvailable(
                    "MetaTrader5 package not importable. It is Windows-only and needs the "
                    "MT5 terminal installed: run the bot on a Windows machine/VPS "
                    "(pip install MetaTrader5)."
                ) from exc
        return self._client

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        client = self._mt5()
        fn = getattr(client, name)
        async with self._call_lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    async def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Serialized terminal call (shared with the market-data feed: MT5 is not thread-safe)."""
        return await self._call(name, *args, **kwargs)

    @property
    def mt5(self) -> Any:
        return self._mt5()

    async def _last_error(self) -> Any:
        try:
            return await self._call("last_error")
        except Exception:  # pragma: no cover - defensive
            return None

    @staticmethod
    def _tag(order_id: str) -> str:
        """Short unique tag for the MT5 comment field (31-char limit)."""
        core = order_id[4:] if order_id.startswith("ord_") else order_id
        return "o" + core[:27]

    def _retcode_name(self, code: int) -> str:
        client = self._mt5()
        for attr in dir(client):
            if attr.startswith("TRADE_RETCODE_") and getattr(client, attr) == code:
                return attr
        return f"RETCODE_{code}"

    def _audit(self, severity: Severity, event: AuditEventType, payload: dict, **kw: Any) -> None:
        audit_log.emit(
            AuditEvent(
                component="mt5",
                event_type=event.value,
                severity=severity,
                payload=payload,
                **kw,
            )
        )

    # ---------------------------------------------------------------- connection

    async def connect(self) -> None:
        s = self._settings
        init_kwargs: dict[str, Any] = {"timeout": int(s.mt5_timeout_ms)}
        if s.mt5_terminal_path:
            init_kwargs["path"] = s.mt5_terminal_path
        if s.mt5_login:
            init_kwargs["login"] = int(s.mt5_login)
            init_kwargs["password"] = s.mt5_password
            init_kwargs["server"] = s.mt5_server
        ok = await self._call("initialize", **init_kwargs)
        if not ok:
            err = await self._last_error()
            # Error code -10003 means "MetaTrader 5 x64 not found" — the package
            # is installed but the terminal is not.  Treat this the same as a
            # missing package so callers get a single, clear exception type.
            if err and err[0] == -10003:
                raise MT5NotAvailable(
                    f"MetaTrader 5 terminal not found: {err[1]}. "
                    "Install the terminal or set MT5_TERMINAL_PATH in .env."
                )
            raise ConnectionError(f"MT5 initialize failed: {err}")

        info = await self._call("account_info")
        if info is None:
            await self._call("shutdown")
            raise ConnectionError("MT5 connected but account_info() returned nothing")

        client = self._mt5()
        is_demo = info.trade_mode == client.ACCOUNT_TRADE_MODE_DEMO
        if not is_demo and not s.is_live:
            await self._call("shutdown")
            self._audit(
                Severity.CRITICAL,
                AuditEventType.MT5_DISCONNECTED,
                {"reason": "real_account_blocked", "login": info.login},
            )
            raise RealAccountBlocked(
                f"Account {info.login} is a REAL-money account but TRADING_MODE is not live "
                "(needs LIVE_TRADING_ENABLED and LIVE_TRADING_ACKNOWLEDGED). Refusing to connect."
            )

        terminal = await self._call("terminal_info")
        if terminal is not None and not getattr(terminal, "trade_allowed", True):
            self._audit(
                Severity.WARNING,
                AuditEventType.MT5_DISCONNECTED,
                {"reason": "algo_trading_disabled_in_terminal"},
            )
        # Publish availability at connect time too: with AutoTrading off the dashboard must say so
        # immediately, not only after the first signal has already been refused by the terminal.
        self._note_execution_availability(await self.terminal_trade_state())
        self._peak_equity = max(float(info.balance), float(info.equity))
        self._connected = True

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._call("shutdown")
        self._connected = False

    async def is_connected(self) -> bool:
        if not self._connected:
            return False
        try:
            terminal = await self._call("terminal_info")
        except Exception:
            return False
        return bool(terminal is not None and getattr(terminal, "connected", False))

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("MT5ExecutionAdapter is not connected")

    # ------------------------------------------------------------- market / specs

    async def _symbol_info(self, symbol: str) -> Any:
        info = await self._call("symbol_info", symbol)
        if info is None:
            await self._call("symbol_select", symbol, True)
            info = await self._call("symbol_info", symbol)
        if info is not None and not getattr(info, "visible", True):
            await self._call("symbol_select", symbol, True)
        return info

    async def get_instrument(self, symbol: str) -> InstrumentSpec | None:
        info = await self._symbol_info(symbol)
        if info is None:
            return None
        try:
            return InstrumentSpec(
                symbol=symbol,
                contract_size=float(info.trade_contract_size),
                tick_size=float(info.trade_tick_size),
                tick_value=float(info.trade_tick_value),
                volume_min=float(info.volume_min),
                volume_max=float(info.volume_max),
                volume_step=float(info.volume_step),
                digits=int(info.digits),
            )
        except (ValueError, TypeError, AttributeError):
            return None  # incomplete spec ⇒ caller treats as unknown (risk engine fails closed)

    # ------------------------------------------------------------------- account

    async def get_account(self) -> AccountSnapshot:
        self._require_connected()
        info = await self._call("account_info")
        if info is None:
            raise RuntimeError(f"account_info() failed: {await self._last_error()}")
        client = self._mt5()

        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        deals = await self._call("history_deals_get", day_start, now + timedelta(days=1)) or []
        realized = 0.0
        for d in deals:
            if d.type in (client.DEAL_TYPE_BUY, client.DEAL_TYPE_SELL):
                realized += float(d.profit) + float(d.commission) + float(d.swap)
                realized += float(getattr(d, "fee", 0.0))
        floating = float(info.profit)

        equity = float(info.equity)
        self._peak_equity = max(self._peak_equity, equity, float(info.balance))
        drawdown = (
            max(0.0, (self._peak_equity - equity) / self._peak_equity * 100.0)
            if self._peak_equity > 0
            else 0.0
        )

        positions = await self._call("positions_get") or []
        exposure = 0.0
        for p in positions:
            sym = await self._symbol_info(p.symbol)
            contract = float(sym.trade_contract_size) if sym is not None else 1.0
            exposure += float(p.volume) * contract * float(p.price_current)

        used_margin = float(info.margin)
        return AccountSnapshot(
            balance=float(info.balance),
            equity=equity,
            free_margin=float(info.margin_free),
            used_margin=used_margin,
            margin_level=float(info.margin_level) if used_margin > 0 else None,
            floating_pnl=floating,
            daily_pnl=realized + floating,
            drawdown_pct=drawdown,
            # Broker truth: every open position counts, including manual/external ones.
            open_positions=len(positions),
            exposure=exposure,
        )

    async def reconcile(self) -> AccountSnapshot:
        return await self.get_account()

    # ----------------------------------------------------------------- positions

    def _to_position(self, p: Any) -> PositionInfo:
        client = self._mt5()
        side = OrderSide.BUY if p.type == client.POSITION_TYPE_BUY else OrderSide.SELL
        return PositionInfo(
            ticket=str(p.ticket),
            order_id=None,
            symbol=str(p.symbol),
            side=side,
            volume=float(p.volume),
            entry_price=float(p.price_open),
            current_price=float(p.price_current),
            floating_pnl=float(p.profit),
            stop_loss=float(p.sl) or None,
            take_profit=float(p.tp) or None,
            opened_at=datetime.fromtimestamp(int(p.time), tz=UTC),
            magic=int(getattr(p, "magic", 0)),
            comment=str(getattr(p, "comment", "")),
            is_external=int(getattr(p, "magic", 0)) != self._settings.mt5_magic,
        )

    async def get_positions(self) -> list[PositionInfo]:
        self._require_connected()
        raw = await self._call("positions_get") or []
        return [self._to_position(p) for p in raw]

    # -------------------------------------------------------------------- orders

    def _filling_mode(self, sym: Any) -> int:
        """Pick a filling mode the symbol accepts.

        Verified against the live Exness terminal with read-only order_check probes:
        ``filling_mode`` there is 3 (FOK|IOC) and ORDER_FILLING_RETURN is answered with
        TRADE_RETCODE_INVALID_FILL (10030) "Unsupported filling mode" because the symbol uses
        MARKET execution. So RETURN is only ever used for non-market execution symbols — sending it
        on a market symbol is a guaranteed rejection, never a fallback.
        """
        client = self._mt5()
        modes = int(getattr(sym, "filling_mode", 0))
        market_execution = int(getattr(sym, "trade_execution", 2)) == 2
        if modes & 2:  # SYMBOL_FILLING_IOC
            return client.ORDER_FILLING_IOC
        if modes & 1:  # SYMBOL_FILLING_FOK
            return client.ORDER_FILLING_FOK
        if market_execution:
            # Neither flag advertised but the symbol is market-executed: IOC is the only mode that
            # can work; RETURN would be rejected with 10030 (probe-verified).
            return client.ORDER_FILLING_IOC
        return client.ORDER_FILLING_RETURN

    async def terminal_trade_state(self) -> dict[str, Any]:
        """Read-only: will the terminal transmit trades at all?

        While the MT5 terminal's AutoTrading button is off, EVERY order_send is refused with
        TRADE_RETCODE_CLIENT_DISABLES_AT (10027) / "AutoTrading disabled by client" — even when the
        request itself is perfectly valid (order_check still answers retcode 0 "Done"). Checking
        this before submitting records the exact reason and avoids spamming the broker with
        orders that cannot be transmitted.
        """
        terminal = await self._call("terminal_info")
        if terminal is None:
            return {
                "available": False,
                "trade_allowed": False,
                "connected": False,
                "reason": "terminal_info_unavailable",
                "expected_retcode": None,
                "expected_retcode_code": None,
            }
        allowed = bool(getattr(terminal, "trade_allowed", False))
        return {
            "available": allowed,
            "trade_allowed": allowed,
            "connected": bool(getattr(terminal, "connected", False)),
            "tradeapi_disabled": bool(getattr(terminal, "tradeapi_disabled", False)),
            "dlls_allowed": bool(getattr(terminal, "dlls_allowed", False)),
            "build": int(getattr(terminal, "build", 0) or 0),
            "reason": "" if allowed else "terminal_autotrading_disabled",
            "expected_retcode": None if allowed else "TRADE_RETCODE_CLIENT_DISABLES_AT",
            "expected_retcode_code": None if allowed else 10027,
            "expected_comment": None if allowed else TERMINAL_BLOCK_COMMENT,
        }

    def _note_execution_availability(self, state: dict[str, Any]) -> None:
        """Remember the last known availability; audit only on transitions (never per attempt)."""
        self._execution_availability = dict(state)
        if state["available"]:
            if self._submit_block is not None:
                self._submit_block = None
                self._audit(
                    Severity.INFO,
                    AuditEventType.EXECUTION_UNBLOCKED,
                    {"reason": "terminal_trade_allowed_restored", "terminal": state},
                )
            return
        if self._submit_block is None:
            self._submit_block = {
                "reason": state["reason"],
                "since": datetime.now(UTC).isoformat(),
                "expected_retcode": state["expected_retcode"],
                "expected_retcode_code": state["expected_retcode_code"],
                "expected_comment": state["expected_comment"],
                "terminal": state,
            }
            self._audit(
                Severity.ERROR,
                AuditEventType.EXECUTION_BLOCKED,
                {
                    "reason": state["reason"],
                    "expected_retcode": state["expected_retcode"],
                    "expected_retcode_code": state["expected_retcode_code"],
                    "expected_comment": state["expected_comment"],
                    "action_required": (
                        "enable AutoTrading (Algo Trading) in the MT5 terminal toolbar; "
                        "no request shape can succeed until then"
                    ),
                    "terminal": state,
                },
            )
        else:
            self._submit_block["attempts_while_blocked"] = (
                int(self._submit_block.get("attempts_while_blocked", 0)) + 1
            )

    def execution_availability(self) -> dict[str, Any]:
        """Explicit execution-availability state for the dashboard/health payload."""
        return {
            "terminal": dict(self._execution_availability),
            "blocked": self._submit_block is not None,
            "block": dict(self._submit_block) if self._submit_block else None,
        }

    def _local_reject(
        self, order: OrderRequest, reason: str, detail: dict[str, Any] | None = None
    ) -> ExecutionRecord:
        """Rejection we can state exactly without (or before) sending anything to the broker."""
        response: dict[str, Any] = {
            "adapter": "mt5",
            "ok": False,
            "local_reject": reason,
            "transmitted": False,
            "credentials_included": False,
        }
        response.update(detail or {})
        record = ExecutionRecord(
            execution_id=new_execution_id(),
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            requested_volume=order.volume,
            requested_price=order.entry,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            mt5_response=response,
            rejection_reason=reason,
            final_status=OrderStatus.BROKER_REJECTED,
            correlation_id=order.correlation_id,
        )
        self.executions.append(record)
        self._audit(
            Severity.ERROR if reason == "terminal_autotrading_disabled" else Severity.WARNING,
            AuditEventType.ORDER_REJECTED,
            {
                "reason": reason,
                "order_id": order.order_id,
                "symbol": order.symbol,
                "retcode": response.get("retcode"),
                "retcode_code": response.get("retcode_code"),
                "comment": response.get("comment"),
                "transmitted": False,
            },
            symbol=order.symbol,
            correlation_id=order.correlation_id,
        )
        return record

    async def _submit_diagnostics(
        self,
        order: OrderRequest,
        request: dict[str, Any],
        sym: Any,
        tick: Any,
        *,
        price: float,
        sl: float,
        tp: float,
        check: Any,
        result: Any,
        terminal: dict[str, Any],
    ) -> dict[str, Any]:
        """Complete, credential-free evidence for one submission attempt.

        Everything needed to explain a broker decision without guessing: the exact request, the
        market at that moment, the broker's symbol contract, the account/terminal permission flags,
        and the raw order_check/order_send answers. Persisted inside the execution record.
        """
        acct = await self._call("account_info")
        point = float(getattr(sym, "point", 0.0) or 0.0)
        spread_points = float(getattr(tick, "spread", 0.0) or 0.0)
        deviation_points = float(self._settings.max_slippage_points)
        deviation_price = deviation_points * point
        warnings: list[str] = []
        if spread_points and point and deviation_price < spread_points * point:
            warnings.append(
                "deviation_price_below_spread: with market execution this can be answered "
                "TRADE_RETCODE_PRICE_OFF (10021) / REQUOTE (10004)"
            )
        stop_distance = abs(price - sl)
        if spread_points and point and stop_distance < spread_points * point:
            warnings.append(
                "stop_distance_below_spread: broker may answer TRADE_RETCODE_INVALID_STOPS (10016)"
            )
        if int(getattr(sym, "trade_mode", 4)) != 4:
            warnings.append("symbol trade_mode is not full trading")
        if not terminal.get("connected", True):
            warnings.append("terminal reports disconnected")

        def _retcode(obj: Any) -> dict[str, Any] | None:
            if obj is None:
                return None
            code = int(getattr(obj, "retcode", -1))
            return {
                "retcode": code,
                "retcode_name": self._retcode_name(code),
                "comment": str(getattr(obj, "comment", "")),
                "margin": float(getattr(obj, "margin", 0.0) or 0.0),
                "margin_free": float(getattr(obj, "margin_free", 0.0) or 0.0),
            }

        return {
            "credentials_included": False,
            "request": dict(request),
            "requested": {
                "symbol": order.symbol,
                "broker_symbol": order.symbol,
                "side": order.side.value,
                "type": int(request.get("type", -1)),
                "volume": float(order.volume),
                "entry_reference": order.entry,
                "price_sent": price,
                "stop_loss": sl,
                "take_profit": tp,
                "deviation_points": deviation_points,
                "deviation_price": deviation_price,
                "magic": int(self._settings.mt5_magic),
                "comment_tag": self._tag(order.order_id),
                "type_time": int(request.get("type_time", -1)),
            },
            "market": {
                "bid": float(getattr(tick, "bid", 0.0) or 0.0),
                "ask": float(getattr(tick, "ask", 0.0) or 0.0),
                "spread_points": spread_points,
                "spread_price": spread_points * point,
            },
            "symbol_spec": {
                "digits": int(getattr(sym, "digits", 0) or 0),
                "point": point,
                "trade_tick_size": float(getattr(sym, "trade_tick_size", 0.0) or 0.0),
                "trade_tick_value": float(getattr(sym, "trade_tick_value", 0.0) or 0.0),
                "trade_contract_size": float(getattr(sym, "trade_contract_size", 0.0) or 0.0),
                "volume_min": float(getattr(sym, "volume_min", 0.0) or 0.0),
                "volume_max": float(getattr(sym, "volume_max", 0.0) or 0.0),
                "volume_step": float(getattr(sym, "volume_step", 0.0) or 0.0),
                "trade_stops_level": int(getattr(sym, "trade_stops_level", 0) or 0),
                "trade_freeze_level": int(getattr(sym, "trade_freeze_level", 0) or 0),
                "filling_mode": int(getattr(sym, "filling_mode", 0) or 0),
                "resolved_filling_mode": self._filling_mode(sym),
                "trade_execution": int(getattr(sym, "trade_execution", -1) or 0),
                "trade_mode": int(getattr(sym, "trade_mode", -1) or 0),
                "trade_calc_mode": int(getattr(sym, "trade_calc_mode", -1) or 0),
                "margin_initial": float(getattr(sym, "margin_initial", 0.0) or 0.0),
                "margin_maintenance": float(getattr(sym, "margin_maintenance", 0.0) or 0.0),
                "spread_float": bool(getattr(sym, "spread_float", False)),
            },
            "account": {
                "trade_allowed": bool(getattr(acct, "trade_allowed", False)),
                "trade_expert": bool(getattr(acct, "trade_expert", False)),
                "margin_mode": int(getattr(acct, "margin_mode", -1)),
                "leverage": int(getattr(acct, "leverage", 0) or 0),
                "equity": float(getattr(acct, "equity", 0.0) or 0.0),
                "margin_free": float(getattr(acct, "margin_free", 0.0) or 0.0),
            },
            "terminal": dict(terminal),
            "order_check": _retcode(check),
            "order_send": _retcode(result),
            "warnings": warnings,
        }

    async def _existing_for_order(self, order_id: str) -> Any | None:
        """Idempotency: an open position already carries this order's tag."""
        tag = self._tag(order_id)
        for p in await self._call("positions_get") or []:
            if p.magic == self._settings.mt5_magic and str(p.comment).startswith(tag):
                return p
        return None

    async def submit_order(self, order: OrderRequest) -> ExecutionRecord:
        self._require_connected()
        if order.status not in {OrderStatus.APPROVED, OrderStatus.SUBMITTED}:
            raise ValueError("Only risk-approved/submitted orders may be executed")
        async with self._trade_lock:
            return await self._submit_locked(order)

    async def _submit_locked(self, order: OrderRequest) -> ExecutionRecord:
        client = self._mt5()

        if order.stop_loss is None:
            return self._local_reject(order, "stop_loss_required")

        existing = await self._existing_for_order(order.order_id)
        if existing is not None:
            self._audit(
                Severity.WARNING,
                AuditEventType.EXECUTION_ERROR,
                {"reason": "duplicate_submit_suppressed", "order_id": order.order_id},
                symbol=order.symbol,
                correlation_id=order.correlation_id,
            )
            record = ExecutionRecord(
                execution_id=new_execution_id(),
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                requested_volume=order.volume,
                requested_price=order.entry,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                mt5_response={"adapter": "mt5", "ok": True, "duplicate_suppressed": True},
                execution_price=float(existing.price_open),
                filled_volume=float(existing.volume),
                slippage=0.0,
                final_status=OrderStatus.FILLED,
                correlation_id=order.correlation_id,
            )
            self.executions.append(record)
            return record

        sym = await self._symbol_info(order.symbol)
        if sym is None:
            return self._local_reject(order, "unknown_symbol")
        spec = await self.get_instrument(order.symbol)
        if spec is None:
            return self._local_reject(order, "instrument_spec_unavailable")
        problems = spec.volume_is_valid(order.volume)
        if problems:
            return self._local_reject(order, problems[0])

        # Terminal-level availability, checked BEFORE any broker traffic. With AutoTrading off,
        # order_send answers 10027 for every request; submitting anyway would just spam the broker
        # and hide the real reason behind a generic "rejected".
        terminal = await self.terminal_trade_state()
        self._note_execution_availability(terminal)
        if not terminal["available"]:
            spec_snapshot = {
                "digits": int(getattr(sym, "digits", 0) or 0),
                "point": float(getattr(sym, "point", 0.0) or 0.0),
                "volume_min": float(getattr(sym, "volume_min", 0.0) or 0.0),
                "volume_max": float(getattr(sym, "volume_max", 0.0) or 0.0),
                "volume_step": float(getattr(sym, "volume_step", 0.0) or 0.0),
                "trade_stops_level": int(getattr(sym, "trade_stops_level", 0) or 0),
                "trade_freeze_level": int(getattr(sym, "trade_freeze_level", 0) or 0),
                "filling_mode": int(getattr(sym, "filling_mode", 0) or 0),
                "resolved_filling_mode": self._filling_mode(sym),
                "trade_execution": int(getattr(sym, "trade_execution", -1) or 0),
                "trade_mode": int(getattr(sym, "trade_mode", -1) or 0),
            }
            return self._local_reject(
                order,
                "terminal_autotrading_disabled",
                detail={
                    "retcode": terminal["expected_retcode"],
                    "retcode_code": terminal["expected_retcode_code"],
                    "comment": terminal["expected_comment"],
                    "broker_verdict_source": "verified by a read-only probe on this terminal",
                    "symbol_spec": spec_snapshot,
                    "requested": {
                        "symbol": order.symbol,
                        "side": order.side.value,
                        "volume": float(order.volume),
                        "entry_reference": order.entry,
                        "stop_loss": order.stop_loss,
                        "take_profit": order.take_profit,
                        "magic": int(self._settings.mt5_magic),
                    },
                    "terminal": terminal,
                },
            )

        tick = await self._call("symbol_info_tick", order.symbol)
        if tick is None or not float(tick.ask) or not float(tick.bid):
            return self._local_reject(order, "no_tick_data")
        is_buy = order.side is OrderSide.BUY
        price = float(tick.ask if is_buy else tick.bid)

        digits = int(sym.digits)
        point = float(sym.point)
        min_dist = float(getattr(sym, "trade_stops_level", 0)) * point
        sl = round(float(order.stop_loss), digits)
        tp = round(float(order.take_profit), digits) if order.take_profit is not None else 0.0

        if (is_buy and sl >= price) or (not is_buy and sl <= price):
            return self._local_reject(order, "stop_wrong_side")
        if abs(price - sl) < min_dist:
            return self._local_reject(order, "stop_too_close")
        if tp:
            if (is_buy and tp <= price) or (not is_buy and tp >= price):
                return self._local_reject(order, "take_profit_wrong_side")
            if abs(tp - price) < min_dist:
                return self._local_reject(order, "take_profit_too_close")

        request = {
            "action": client.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": float(order.volume),
            "type": client.ORDER_TYPE_BUY if is_buy else client.ORDER_TYPE_SELL,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": int(self._settings.max_slippage_points),
            "magic": int(self._settings.mt5_magic),
            "comment": self._tag(order.order_id),
            "type_time": client.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(sym),
        }

        check = await self._call("order_check", request)
        diagnostics = await self._submit_diagnostics(
            order,
            request,
            sym,
            tick,
            price=price,
            sl=sl,
            tp=tp,
            check=check,
            result=None,
            terminal=terminal,
        )
        if check is None or int(check.retcode) != 0:
            code = int(check.retcode) if check is not None else -1
            comment = getattr(check, "comment", "") if check is not None else "no_result"
            return self._local_reject(
                order,
                f"order_check_failed:{code}:{comment}",
                detail={
                    "retcode": self._retcode_name(code) if code >= 0 else None,
                    "retcode_code": code,
                    "comment": str(comment),
                    "diagnostics": diagnostics,
                },
            )

        result = await self._call("order_send", request)
        if result is None:
            raise RuntimeError(f"order_send returned None: {await self._last_error()}")

        code = int(result.retcode)
        name = self._retcode_name(code)
        response = {
            "adapter": "mt5",
            "retcode": name,
            "retcode_code": code,
            "order": int(getattr(result, "order", 0)),
            "deal": int(getattr(result, "deal", 0)),
            "comment": str(getattr(result, "comment", "")),
            "request_id": int(getattr(result, "request_id", 0)),
            "transmitted": True,
            "credentials_included": False,
            "diagnostics": {
                **diagnostics,
                "order_send": {
                    "retcode": code,
                    "retcode_name": name,
                    "comment": str(getattr(result, "comment", "")),
                    "order": int(getattr(result, "order", 0) or 0),
                    "deal": int(getattr(result, "deal", 0) or 0),
                    "request_id": int(getattr(result, "request_id", 0) or 0),
                },
            },
        }

        if code in (client.TRADE_RETCODE_DONE, client.TRADE_RETCODE_DONE_PARTIAL):
            filled = float(result.volume) or float(order.volume)
            exec_price = float(result.price) or price
            partial = code == client.TRADE_RETCODE_DONE_PARTIAL or filled < order.volume - 1e-9
            record = ExecutionRecord(
                execution_id=new_execution_id(),
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                requested_volume=order.volume,
                requested_price=price,
                stop_loss=sl,
                take_profit=tp or None,
                mt5_response={**response, "ok": True},
                execution_price=exec_price,
                filled_volume=filled,
                slippage=abs(exec_price - price),
                final_status=OrderStatus.PARTIALLY_FILLED if partial else OrderStatus.FILLED,
                correlation_id=order.correlation_id,
            )
            self.executions.append(record)
            self._audit(
                Severity.INFO,
                AuditEventType.POSITION_OPENED,
                {"order_id": order.order_id, "ticket": response["order"], "price": exec_price},
                symbol=order.symbol,
                correlation_id=order.correlation_id,
            )
            return record

        if name in _DEFINITE_REJECTS:
            record = ExecutionRecord(
                execution_id=new_execution_id(),
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                requested_volume=order.volume,
                requested_price=price,
                stop_loss=sl,
                take_profit=tp or None,
                mt5_response={**response, "ok": False},
                rejection_reason=name,
                final_status=OrderStatus.BROKER_REJECTED,
                correlation_id=order.correlation_id,
            )
            self.executions.append(record)
            return record

        # Uncertain outcome (timeout, connection loss, unknown code): the order MAY exist.
        # Raise so the order manager marks it FAILED; reconcile() then resolves it from
        # broker truth via the order tag.
        raise RuntimeError(f"uncertain order outcome: {name} ({response['comment']})")

    async def modify_position(
        self,
        ticket: str,
        *,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> ExecutionRecord:
        self._require_connected()
        client = self._mt5()
        async with self._trade_lock:
            found = await self._call("positions_get", ticket=int(ticket)) or []
            if not found:
                raise KeyError(f"unknown position: {ticket}")
            pos = found[0]
            request = {
                "action": client.TRADE_ACTION_SLTP,
                "symbol": str(pos.symbol),
                "position": int(pos.ticket),
                "sl": float(stop_loss or 0.0),
                "tp": float(take_profit or 0.0),
                "magic": int(self._settings.mt5_magic),
                "comment": "position_modify",
            }
            result = await self._call("order_send", request)
            if result is None:
                raise RuntimeError(f"order_send returned None: {await self._last_error()}")
            code = int(result.retcode)
            done = code == client.TRADE_RETCODE_DONE
            record = ExecutionRecord(
                execution_id=new_execution_id(),
                order_id="",
                symbol=str(pos.symbol),
                side=self._to_position(pos).side,
                requested_volume=float(pos.volume),
                stop_loss=stop_loss,
                take_profit=take_profit,
                mt5_response={
                    "adapter": "mt5",
                    "ok": done,
                    "action": "modify",
                    "position": int(pos.ticket),
                },
                rejection_reason=None if done else self._retcode_name(code),
                final_status=OrderStatus.FILLED if done else OrderStatus.BROKER_REJECTED,
                correlation_id=str(pos.ticket),
            )
            self.executions.append(record)
            return record

    async def close_position(self, ticket: str, *, volume: float | None = None) -> ExecutionRecord:
        self._require_connected()
        client = self._mt5()
        async with self._trade_lock:
            found = await self._call("positions_get", ticket=int(ticket)) or []
            if not found:
                raise KeyError(f"unknown position: {ticket}")
            pos = found[0]
            is_buy = pos.type == client.POSITION_TYPE_BUY
            sym = await self._symbol_info(pos.symbol)
            tick = await self._call("symbol_info_tick", pos.symbol)
            if sym is None or tick is None:
                raise RuntimeError(f"no market data to close {pos.symbol}")
            price = float(tick.bid if is_buy else tick.ask)
            close_volume = float(pos.volume) if volume is None else float(volume)
            if close_volume <= 0 or close_volume > float(pos.volume):
                raise ValueError(f"invalid close volume {close_volume} for position {ticket}")
            request = {
                "action": client.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": close_volume,
                "type": client.ORDER_TYPE_SELL if is_buy else client.ORDER_TYPE_BUY,
                "position": int(pos.ticket),
                "price": price,
                "deviation": int(self._settings.max_slippage_points),
                "magic": int(self._settings.mt5_magic),
                "comment": "close",
                "type_time": client.ORDER_TIME_GTC,
                "type_filling": self._filling_mode(sym),
            }
            result = await self._call("order_send", request)
            if result is None:
                raise RuntimeError(f"order_send returned None: {await self._last_error()}")
            code = int(result.retcode)
            name = self._retcode_name(code)
            done = code in (client.TRADE_RETCODE_DONE, client.TRADE_RETCODE_DONE_PARTIAL)
            exec_price = float(result.price) or price
            record = ExecutionRecord(
                execution_id=new_execution_id(),
                order_id="",
                symbol=str(pos.symbol),
                side=OrderSide.SELL if is_buy else OrderSide.BUY,
                requested_volume=close_volume,
                requested_price=price,
                mt5_response={
                    "adapter": "mt5",
                    "ok": done,
                    "action": "close",
                    "retcode": name,
                    "position": int(pos.ticket),
                },
                execution_price=exec_price if done else None,
                filled_volume=float(result.volume) if done else None,
                slippage=abs(exec_price - price) if done else None,
                rejection_reason=None if done else name,
                final_status=OrderStatus.CLOSED if done else OrderStatus.BROKER_REJECTED,
                correlation_id=str(pos.ticket),
            )
            self.executions.append(record)
            if done:
                self._audit(
                    Severity.INFO,
                    AuditEventType.POSITION_CLOSED,
                    {"ticket": int(pos.ticket), "price": exec_price},
                    symbol=str(pos.symbol),
                )
            return record

    async def position_close_details(self, ticket: str) -> dict[str, Any] | None:
        """Broker truth for a closed position: closing deals give real money and the exit price.

        Read-only and best effort: returns None when the broker has no closing deal for the ticket,
        so the recorder records "unavailable" instead of an invented number.
        """
        self._require_connected()
        client = self._mt5()
        now = datetime.now(UTC)
        deals = (
            await self._call("history_deals_get", now - timedelta(days=30), now + timedelta(days=1))
            or []
        )
        outs = [
            d
            for d in deals
            if getattr(d, "entry", None) == client.DEAL_ENTRY_OUT
            and int(getattr(d, "position_id", 0) or 0) == int(ticket)
        ]
        volume = sum(float(d.volume) for d in outs)
        if not outs or volume <= 0:
            return None
        profit = sum(float(d.profit) for d in outs)
        commission = sum(float(d.commission) for d in outs)
        swap = sum(float(d.swap) for d in outs)
        fee = sum(float(getattr(d, "fee", 0.0)) for d in outs)
        close_times = [int(d.time) for d in outs if getattr(d, "time", None)]
        return {
            "exit_price": sum(float(d.price) * float(d.volume) for d in outs) / volume,
            "volume": volume,
            "profit": profit,
            # Net realized money, consistent with this adapter's daily-P/L calculation.
            "realized_pnl": profit + commission + swap + fee,
            "commission": commission,
            "swap": swap,
            "closed_at": (
                datetime.fromtimestamp(max(close_times), tz=UTC) if close_times else None
            ),
            "deals": [str(getattr(d, "ticket", "")) for d in outs],
            "source": "broker_history",
        }

    async def broker_order_states(self, order_ids: list[str]) -> dict[str, str]:
        """Broker truth for our orders, found via the order tag in the MT5 comment."""
        self._require_connected()
        client = self._mt5()
        magic = self._settings.mt5_magic
        positions = await self._call("positions_get") or []
        open_tags = {str(p.comment)[:28] for p in positions if p.magic == magic}
        now = datetime.now(UTC)
        deals = (
            await self._call("history_deals_get", now - timedelta(days=30), now + timedelta(days=1))
            or []
        )
        closed_tags = {
            str(d.comment)[:28]
            for d in deals
            if d.magic == magic and d.entry == client.DEAL_ENTRY_IN
        }
        states: dict[str, str] = {}
        for order_id in order_ids:
            tag = self._tag(order_id)
            if tag in open_tags:
                states[order_id] = OrderStatus.FILLED.value
            elif tag in closed_tags:
                states[order_id] = OrderStatus.CLOSED.value
        return states
