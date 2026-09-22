"""Print the research report as a ranked table."""

import json
from pathlib import Path

path = Path(r"C:\mt5bbot\data\research_report.json")
data = json.loads(path.read_text(encoding="utf-8"))

print(f"Symbol: {data['symbol']}  Timeframe: {data['timeframe']}")
d0 = data["data_range"][0][:10]
d1 = data["data_range"][1][:10]
print(f"Data: {d0} to {d1}  ({data['total_bars']} bars)")
print(f"Passed: {data['passed']} / {len(data['candidates'])}")
print()
hdr = (
    f"{'Name':<22}{'ISt':>5}{'IS_PF':>7}{'IS_E':>8}"
    f"{'OOS t':>6}{'OOS_PF':>8}{'OOS_E':>8}"
    f"{'WF':>6}{'MC':>6}{'Spr':>5}{'Prm':>5}  Status"
)
print(hdr)
print("-" * len(hdr))
for c in sorted(data["candidates"], key=lambda x: x["oos_profit_factor"], reverse=True):
    status = "PASS" if c["validation_passed"] else "FAIL"
    print(
        f"{c['name']:<22}{c['is_trades']:>5}{c['is_profit_factor']:>7.2f}"
        f"{c['is_expectancy']:>8.2f}{c['oos_trades']:>6}"
        f"{c['oos_profit_factor']:>8.2f}"
        f"{c['oos_expectancy']:>8.2f}{c['wf_pass_rate']:>6.0%}"
        f"{c['mc_pct_profitable']:>6.0%}{str(c['spread_stable'])[0]:>5}"
        f"{str(c['param_stable'])[0]:>5}  {status}"
    )
print()
print("FAILURE REASONS:")
for c in data["candidates"]:
    if not c["validation_passed"]:
        print(f"  {c['name']}: {'; '.join(c['rejection_reasons'])}")
