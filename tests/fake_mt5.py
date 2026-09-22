"""In-memory stand-in for the MetaTrader5 module (constants + the calls the adapter uses)."""

from __future__ import annotations

import time
from types import SimpleNamespace


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    DEAL_TYPE_BALANCE = 2
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TIMEFRAME_M1 = 1
    TIMEFRAME_M15 = 15
    TRADE_RETCODE_REQUOTE = 10004
    TRADE_RETCODE_REJECT = 10006
    TRADE_RETCODE_INVALID_VOLUME = 10014
    TRADE_RETCODE_INVALID_PRICE = 10015
    TRADE_RETCODE_INVALID_STOPS = 10016
    TRADE_RETCODE_TRADE_DISABLED = 10017
    TRADE_RETCODE_MARKET_CLOSED = 10018
    TRADE_RETCODE_NO_MONEY = 10019
    TRADE_RETCODE_PRICE_CHANGED = 10020
    TRADE_RETCODE_PRICE_OFF = 10021
    TRADE_RETCODE_CLIENT_DISABLES_AT = 10027
    TRADE_RETCODE_INVALID_FILL = 10030
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_TIMEOUT = 10012

    def __init__(self, *, trade_mode: int = 0, balance: float = 10_000.0) -> None:
        self.trade_mode = trade_mode
        self.balance = balance
        self.profit = 0.0
        self.margin = 0.0
        self.positions: list[SimpleNamespace] = []
        self.deals: list[SimpleNamespace] = []
        self.sent: list[dict] = []
        self.checked: list[dict] = []
        self.next_check_retcode = 0
        self.next_send_retcode = self.TRADE_RETCODE_DONE
        self.send_returns_none = False
        self.record_position_on_uncertain = False
        # Terminal/account permission flags (AutoTrading button state is terminal-wide).
        self.trade_allowed = True
        self.tradeapi_disabled = False
        self.dlls_allowed = False
        self.build = 6205
        self.send_comment = "fake"
        self.tick = SimpleNamespace(bid=2500.00, ask=2500.30, time=1, time_msc=1000)
        self.rates: list[dict] = []
        self.symbol = SimpleNamespace(
            digits=2,
            point=0.01,
            trade_stops_level=10,
            trade_contract_size=100.0,
            trade_tick_size=0.01,
            trade_tick_value=1.0,
            volume_min=0.01,
            volume_max=50.0,
            volume_step=0.01,
            filling_mode=2,
            visible=True,
        )
        self._ticket = 1000
        self.initialized_with: dict = {}

    # --- lifecycle
    def initialize(self, **kwargs):
        self.initialized_with = kwargs
        return True

    def shutdown(self):
        return True

    def last_error(self):
        return (1, "fake")

    def terminal_info(self):
        return SimpleNamespace(
            connected=True,
            trade_allowed=self.trade_allowed,
            tradeapi_disabled=self.tradeapi_disabled,
            dlls_allowed=self.dlls_allowed,
            build=self.build,
        )

    def account_info(self):
        equity = self.balance + self.profit
        return SimpleNamespace(
            login=123456,
            trade_mode=self.trade_mode,
            balance=self.balance,
            equity=equity,
            profit=self.profit,
            margin=self.margin,
            margin_free=equity - self.margin,
            margin_level=(equity / self.margin * 100) if self.margin else 0.0,
        )

    # --- market
    def symbol_info(self, symbol):
        return self.symbol if symbol == "XAUUSD" else None

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info_tick(self, symbol):
        return self.tick

    # --- positions / history
    def positions_get(self, ticket=None, **_):
        if ticket is not None:
            return tuple(p for p in self.positions if p.ticket == ticket)
        return tuple(self.positions)

    def history_deals_get(self, date_from, date_to, **_):
        return tuple(self.deals)

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        rows = self.rates[: len(self.rates) - start_pos] if start_pos else self.rates
        return rows[-count:] if rows else None

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        return self.rates or None

    # --- trading
    def order_check(self, request):
        self.checked.append(request)
        return SimpleNamespace(retcode=self.next_check_retcode, comment="check")

    def order_send(self, request):
        self.sent.append(request)
        if self.send_returns_none:
            return None
        code = self.next_send_retcode
        if request.get("action") == self.TRADE_ACTION_SLTP:
            for pos in self.positions:
                if pos.ticket == request["position"]:
                    pos.sl, pos.tp = request["sl"], request["tp"]
            return SimpleNamespace(
                retcode=code,
                order=self._next(),
                deal=0,
                volume=0.0,
                price=0.0,
                comment="modified",
                request_id=1,
            )
        if "position" in request:  # close: full close removes, partial close reduces volume
            remaining: list[SimpleNamespace] = []
            closed_pos = None
            for pos in self.positions:
                if pos.ticket == request["position"]:
                    closed_pos = pos
                    left = float(pos.volume) - float(request["volume"])
                    if left > 0:
                        pos.volume = left
                        remaining.append(pos)
                    continue
                remaining.append(pos)
            self.positions = remaining
            if closed_pos is not None and float(request["volume"]) > 0:
                # Real MT5 records a DEAL_ENTRY_OUT deal per close: the fake does the same so
                # closing-deal based P/L (adapter.position_close_details) is testable.
                contract = float(getattr(self.symbol, "trade_contract_size", 100.0))
                price = float(request["price"])
                volume = float(request["volume"])
                direction = 1.0 if closed_pos.type == self.DEAL_TYPE_BUY else -1.0
                self.deals.append(
                    SimpleNamespace(
                        ticket=self._next(),
                        position_id=int(request["position"]),
                        type=request["type"],
                        entry=self.DEAL_ENTRY_OUT,
                        magic=int(getattr(closed_pos, "magic", 0) or 0),
                        comment=str(getattr(closed_pos, "comment", "")),
                        price=price,
                        volume=volume,
                        profit=(
                            (price - float(closed_pos.price_open)) * direction * volume * contract
                        ),
                        commission=0.0,
                        swap=0.0,
                        fee=0.0,
                        time=int(time.time()),
                    )
                )
            return SimpleNamespace(
                retcode=code,
                order=self._next(),
                deal=self._next(),
                volume=request["volume"],
                price=request["price"],
                comment=self.send_comment,
                request_id=1,
            )
        opened = code in (self.TRADE_RETCODE_DONE, self.TRADE_RETCODE_DONE_PARTIAL) or (
            self.record_position_on_uncertain
        )
        ticket = self._next()
        if opened:
            self.positions.append(
                SimpleNamespace(
                    ticket=ticket,
                    symbol=request["symbol"],
                    volume=request["volume"],
                    type=request["type"],
                    price_open=request["price"],
                    price_current=request["price"],
                    profit=0.0,
                    sl=request["sl"],
                    tp=request["tp"],
                    magic=request["magic"],
                    comment=request["comment"],
                    time=int(time.time()),
                )
            )
            self.deals.append(
                SimpleNamespace(
                    type=request["type"],
                    entry=self.DEAL_ENTRY_IN,
                    magic=request["magic"],
                    comment=request["comment"],
                    profit=0.0,
                    commission=0.0,
                    swap=0.0,
                    fee=0.0,
                )
            )
        return SimpleNamespace(
            retcode=code,
            order=ticket,
            deal=self._next(),
            volume=request["volume"],
            price=request["price"],
            comment=self.send_comment,
            request_id=1,
        )

    def _next(self) -> int:
        self._ticket += 1
        return self._ticket
