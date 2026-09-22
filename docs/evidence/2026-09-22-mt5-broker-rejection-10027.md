# MT5 broker rejection of the autonomous BUY — root cause (2026-09-22)

## The two rejected autonomous entries

From the persistent database (`signals`, `orders`, both written to Aiven Postgres):

| time (UTC) | side | volume | entry | SL | TP | order status |
| --- | --- | --- | --- | --- | --- | --- |
| 12:30:01 | BUY XAUUSDm | 0.02 | 4336.919 | 4315.234 | 4380.288 | `rejected` (risk gate — never sent to the broker) |
| 13:15:02 | BUY XAUUSDm | 0.02 | 4340.664 | 4318.961 | 4384.071 | `approved` in the DB, `BROKER_REJECTED` in memory |

The 13:15 attempt is the one that reached the broker. Its raw retcode was **not** in the database,
because of a second bug (see below), so the cause was established with read-only probes against
the live terminal instead of guesswork.

## Read-only probe results (nothing was sent; `order_check` never places an order)

Terminal (build 6205, `Exness-MT5Trial9`, demo):

```
connected: true   trade_allowed: false   tradeapi_disabled: false   dlls_allowed: false
account: trade_allowed true, trade_expert true, leverage 500, balance/equity ~10,000,000 (USD)
```

Symbol `XAUUSDm` contract as reported by the broker:

```
digits 3, point 0.001, trade_tick_size 0.001, trade_tick_value 0.1, contract size 100
volume_min 0.01, volume_max 200.0, volume_step 0.01
trade_stops_level 0, trade_freeze_level 0
filling_mode 3 (FOK|IOC), trade_execution 2 (market), trade_mode 4 (full)
tick bid 4333.194 / ask 4333.454 -> spread 260 points
```

`order_check` for the exact request the adapter builds (BUY 0.02, price = ask, SL −21.70,
TP +43.41, IOC filling, deviation 25 points):

```
as the adapter builds it      -> retcode 0     "Done"                     margin 17.33
FOK / IOC filling             -> retcode 0     "Done"                     margin 17.33
ORDER_FILLING_RETURN          -> retcode 10030 "Unsupported filling mode"
SL/TP only 0.01 away          -> retcode 10016 "Invalid stops"
```

Decisive probe (a `TRADE_ACTION_SLTP` on non-existent position 0 — it cannot open or close
anything, but the terminal still answers with its own permission verdict):

```
retcode 10027 (TRADE_RETCODE_CLIENT_DISABLES_AT)  comment "AutoTrading disabled by client"
```

## Root cause

**The MT5 terminal's AutoTrading (Algo Trading) button is off** (`terminal_info.trade_allowed =
false`), so every `order_send` is refused with **10027 TRADE_RETCODE_CLIENT_DISABLES_AT / "AutoTrading
disabled by client"**. The order *request* was valid — `order_check` answers `0 "Done"` for exactly
that request, against the real contract values, so volume, price side, SL/TP distance and filling
mode are all acceptable to the broker. Nothing about the strategy, the risk gate, sizing or the
stop placement caused this.

## Second, independent defect (why the reason was invisible)

`executions` holds no row for either live attempt and `orders` kept the pre-submit status:
`SqlAlchemyMarketDataStore.write_order()` inserted instead of upserting, so **every status
transition after the first write raised a duplicate-key error**. Consequences:

* the broker's retcode/comment was never persisted (it only existed in memory, lost on restart);
* the loop counted a cycle error instead of recording the rejection;
* the dashboard's "REJECTED" label was all that survived.

Fixed: `write_order` and `write_execution` now upsert, and the stored order snapshot carries
`status`, `rejection_reason` and `filled_volume`.

## Adapter fixes (no strategy, risk or limit changes)

1. **Pre-flight terminal availability** before any broker traffic. While AutoTrading is off the
   submit returns an explicit, non-transmitted rejection carrying the exact broker verdict
   (10027 + "AutoTrading disabled by client"), the symbol contract and the request, and it is
   published at *connect* time so the dashboard shows the block before the first signal.
2. **No broker spam**: one `EXECUTION_BLOCKED` audit event per outage (further attempts only
   increment `attempts_while_blocked`), plus `EXECUTION_UNBLOCKED` when the terminal allows trades
   again. Definite rejects are still never retried.
3. **Filling-mode contract**: `ORDER_FILLING_RETURN` is never used for market-execution symbols
   (probe-verified 10030), only for non-market symbols that advertise neither FOK nor IOC.
4. **Full safe diagnostics on every attempt** (persisted in the execution record): request
   (price/SL/TP/volume/filling/deviation/magic/GTC), market (bid/ask/spread), broker contract
   (digits, point, tick size/value, contract size, volume min/max/step, stops/freeze level,
   filling_mode + resolved mode, trade_execution, trade_mode, margin), account and terminal
   permission flags, raw `order_check` and `order_send` retcode/comment/margin — and explicit
   warnings (deviation below spread → expect 10021/10004; stop distance below spread → expect
   10016). Credentials are never included (asserted by test).

## Operator action required

Enable **AutoTrading / Algo Trading** in the MT5 terminal toolbar. No request shape can succeed
until then; the runtime now states this instead of reporting a mystery rejection.

## Regression tests

`tests/test_execution_rejection.py` locks in the real Exness `XAUUSDm` values above: request shape
validity, IOC selection on a market symbol, that RETURN is never used there, AutoTrading-off
blocking without broker traffic (one audit event, attempt counting, recovery), exact 10027/10030/10016
reporting, credential-free diagnostics completeness, and the order/execution persistence regression
(single row per order, updated in place, rejection reason stored).
