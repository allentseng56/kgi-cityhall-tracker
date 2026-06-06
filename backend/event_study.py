# -*- coding: utf-8 -*-
"""
Event study / backtest: after a 凱基市府 "重大買訊", how does the stock perform?

For every (signal_date, stock_id) that qualifies as a buy signal, we measure
forward performance using price_history:
  - forward return at t+1, t+3, t+5, t+10, t+20 (trading days)
  - days-to-peak within the window
  - peak return within the window
  - days held above entry (how long the rise lasts)

Entry price = the NEXT trading day's OPEN after the signal (realistic: branch
data is published after close, so the earliest you can act is next open).
All returns are raw (not market-adjusted).

Signal definitions (choose with --signal):
  purebuy   : sell_shares==0 and buy_shares>=MIN_SHARES        (default)
  strongbuy : buy_shares >= 5 * sell_shares and net>=MIN_SHARES
  topnet    : that day's top-N by net_shares

Usage:
    python event_study.py
    python event_study.py --signal strongbuy --windows 1,3,5,10,20
"""
import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db

MIN_SHARES = 10000      # 10 張 floor for "重大" (filters out noise like 100 股)


def get_signals(signal_type, top_n=10):
    """Return list of (trade_date, stock_id, stock_name) qualifying signals."""
    with db.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM kgi_cityhall_daily").fetchall()]
    sig = []
    if signal_type == "purebuy":
        for r in rows:
            if r["sell_shares"] == 0 and r["buy_shares"] >= MIN_SHARES:
                sig.append((r["trade_date"], r["stock_id"], r["stock_name"]))
    elif signal_type == "strongbuy":
        for r in rows:
            if (r["buy_shares"] >= 5 * max(r["sell_shares"], 1)
                    and r["net_shares"] >= MIN_SHARES):
                sig.append((r["trade_date"], r["stock_id"], r["stock_name"]))
    elif signal_type == "topnet":
        from collections import defaultdict
        by_date = defaultdict(list)
        for r in rows:
            by_date[r["trade_date"]].append(r)
        for d, rs in by_date.items():
            rs.sort(key=lambda x: x["net_shares"], reverse=True)
            for r in rs[:top_n]:
                if r["net_shares"] >= MIN_SHARES:
                    sig.append((d, r["stock_id"], r["stock_name"]))
    return sig


def forward_metrics(stock_id, signal_date, windows):
    """
    Compute forward performance for one event. Returns dict or None.
    Entry = next trading day's OPEN after the signal.
    Holding window w means the close on the w-th forward trading day.
    """
    prices = db.query_prices(stock_id)
    if not prices:
        return None
    dates = [p["trade_date"] for p in prices]
    if signal_date not in dates:
        return None
    i0 = dates.index(signal_date)

    forward = prices[i0 + 1:]   # strictly after signal day
    if not forward:
        return None
    entry = forward[0]["open"]  # realistic entry: next-day open
    if not entry:
        # fall back to next-day close if open missing
        entry = forward[0]["close"]
    if not entry:
        return None

    result = {"stock_id": stock_id, "signal_date": signal_date,
              "entry": entry, "entry_date": forward[0]["trade_date"],
              "n_forward": len(forward)}

    # Window returns: close on the w-th forward day vs entry open
    for w in windows:
        if len(forward) >= w:
            px = forward[w - 1]["close"]
            result[f"ret_{w}"] = (px / entry - 1) * 100 if px else None
        else:
            result[f"ret_{w}"] = None

    # Peak within max window
    maxw = max(windows)
    window_slice = forward[:maxw]
    highs = [(j + 1, p["high"] or p["close"]) for j, p in enumerate(window_slice)
             if (p["high"] or p["close"])]
    if highs:
        peak_day, peak_px = max(highs, key=lambda t: t[1])
        result["peak_ret"] = (peak_px / entry - 1) * 100
        result["days_to_peak"] = peak_day
        # Days held above entry (consecutive closes > entry from t+1)
        held = 0
        for p in window_slice:
            if p["close"] and p["close"] > entry:
                held += 1
            else:
                break
        result["days_above_entry"] = held
        result["rose"] = result["peak_ret"] > 0
    return result


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def run(signal_type="purebuy", windows=(1, 3, 5, 10, 20)):
    db.init_db()
    signals = get_signals(signal_type)

    print(f"=== 事件研究：訊號類型 = {signal_type} ===")
    print(f"訊號門檻：≥ {MIN_SHARES:,} 股（{MIN_SHARES//1000} 張）")
    print(f"進場價：訊號隔日開盤；報酬為原始報酬（未扣市場基準）")
    print(f"符合訊號的事件數：{len(signals)}")
    sig_dates = sorted(set(s[0] for s in signals))
    print(f"涵蓋訊號日：{sig_dates}")
    print()

    events = []
    for (d, sid, name) in signals:
        m = forward_metrics(sid, d, windows)
        if m:
            m["name"] = name
            events.append(m)

    if not events:
        print("⚠️ 沒有可用事件（可能股價歷史尚未回補，或訊號日無後續股價）。")
        print("   請先執行：python update_prices.py")
        return

    print(f"可分析事件數（有後續股價）：{len(events)}")
    max_fwd = max(e["n_forward"] for e in events)
    print(f"訊號日後最多有 {max_fwd} 個交易日的觀察期")
    print()

    # ---- Aggregate window returns ----
    print("【各持有天數的報酬率】（進場 = 訊號隔日開盤價）")
    print(f"{'持有天數':<8}{'平均報酬':>10}{'中位數':>10}{'勝率':>8}{'樣本':>6}")
    for w in windows:
        rets = [e.get(f"ret_{w}") for e in events]
        valid = [r for r in rets if r is not None]
        if not valid:
            print(f"t+{w:<6}{'資料不足':>10}")
            continue
        win = sum(1 for r in valid if r > 0) / len(valid) * 100
        print(f"t+{w:<6}{_avg(valid):>+9.2f}%{_median(valid):>+9.2f}%{win:>7.0f}%{len(valid):>6}")
    print()

    # ---- Peak / duration ----
    peak_rets = [e.get("peak_ret") for e in events]
    days_to_peak = [e.get("days_to_peak") for e in events]
    days_above = [e.get("days_above_entry") for e in events]
    rose = [e.get("rose") for e in events if e.get("rose") is not None]

    print(f"【峰值與上漲持續性】（觀察窗 = 最多 t+{max(windows)}）")
    if any(r is not None for r in peak_rets):
        print(f"  平均最大漲幅：{_avg(peak_rets):+.2f}%（中位數 {_median(peak_rets):+.2f}%）")
        print(f"  平均幾天後到達峰值：{_avg(days_to_peak):.1f} 天（中位數 {_median(days_to_peak):.0f} 天）")
        print(f"  平均連續站上進場價天數：{_avg(days_above):.1f} 天")
    if rose:
        print(f"  曾在觀察窗內上漲的比例：{sum(rose)/len(rose)*100:.0f}%")
    print()

    # ---- Per-event detail ----
    print("【個別事件明細】")
    hdr = f"{'訊號日':<12}{'股票':<14}{'進場價':>8}"
    for w in windows:
        hdr += f"{'t+'+str(w):>8}"
    hdr += f"{'峰值%':>8}{'到峰天':>7}"
    print(hdr)
    for e in sorted(events, key=lambda x: x.get("peak_ret") or -999, reverse=True):
        line = f"{e['signal_date']:<12}{e['stock_id']+' '+e['name']:<14}{e['entry']:>8.2f}"
        for w in windows:
            v = e.get(f"ret_{w}")
            line += f"{(f'{v:+.1f}' if v is not None else '--'):>8}"
        pk = e.get("peak_ret"); dp = e.get("days_to_peak")
        line += f"{(f'{pk:+.1f}' if pk is not None else '--'):>8}"
        line += f"{(str(dp) if dp else '--'):>7}"
        print(line)

    print()
    print("⚠️ 注意事項：")
    print(f"  1. 目前僅 {len(sig_dates)} 個訊號日，樣本量小，結論僅供參考、不具統計顯著性。")
    print("  2. 進場價為訊號隔日開盤；報酬為原始報酬，未扣大盤共同漲跌。")
    print("  3. 需累積更多訊號日（或回補 FinMind 付費分點歷史）才能得到可信的統計結論。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", default="purebuy",
                    choices=["purebuy", "strongbuy", "topnet"])
    ap.add_argument("--windows", default="1,3,5,10,20")
    args = ap.parse_args()
    windows = tuple(int(x) for x in args.windows.split(","))
    run(signal_type=args.signal, windows=windows)
