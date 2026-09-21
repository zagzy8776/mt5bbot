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
            raise ConnectionError(f"MT5 initialize failed: {await self._last_error()}")

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
                symbol=symbol.upper(),
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
            open_positions=len([p for p in positions if p.magic == self._settings.mt5_magic]),
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
            symbol=str(p.symbol).upper(),
            side=side,
            volume=float(p.volume),
            entry_price=float(p.price_open),
            current_price=float(p.price_current),
            floating_pnl=float(p.profit),
            stop_loss=float(p.sl) or None,
            take_profit=float(p.tp) or None,
            opened_at=datetime.fromtimestamp(int(p.time), tz=UTC),
        )

    async def get_positions(self) -> list[PositionInfo]:
        self._require_connected()
        raw = await self._call("positions_get") or []
        return [self._to_position(p) for p in raw if p.magic == self._settings.mt5_magic]

    # -------------------------------------------------------------------- orders

    def _filling_mode(self, sym: Any) -> int:
        client = self._mt5()
        modes = int(getattr(sym, "filling_mode", 0))
        if modes & 2:  # SYMBOL_FILLING_IOC
            return client.ORDER_FILLING_IOC
        if modes & 1:  # SYMBOL_FILLING_FOK
            return client.ORDER_FILLING_FOK
        return client.ORDER_FILLING_RETURN

    def _local_reject(self, order: OrderRequest, reason: str) -> ExecutionRecord:
        record = ExecutionRecord(
            execution_id=new_execution_id(),
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            requested_volume=order.volume,
            requested_price=order.entry,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            mt5_response={"adapter": "mt5", "ok": False, "local_reject": reason},
            rejection_reason=reason,
            final_status=OrderStatus.BROKER_REJECTED,
            correlation_id=order.correlation_id,
        )
        self.executions.append(record)
        return record

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
        if check is None or int(check.retcode) != 0:
            code = int(check.retcode) if check is not None else -1
            comment = getattr(check, "comment", "") if check is not None else "no_result"
            return self._local_reject(order, f"order_check_failed:{code}:{comment}")

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

    async def close_position(self, ticket: str) -> ExecutionRecord:
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
            request = {
                "action": client.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": float(pos.volume),
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
                symbol=str(pos.symbol).upper(),
                side=OrderSide.SELL if is_buy else OrderSide.BUY,
                requested_volume=float(pos.volume),
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
                    symbol=str(pos.symbol).upper(),
                )
            return record

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
