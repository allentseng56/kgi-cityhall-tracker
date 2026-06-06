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


def path_metrics(stock_id, signal_date, window):
    """
    路徑分析（不固定持有天數）：追蹤進場後的價格路徑，找出自然獲利週期。
    進場 = 訊號隔日開盤。觀察窗 = window 個交易日。
    回傳：
      ever_profit          觀察窗內是否曾獲利（收盤 > 進場）
      days_to_first_profit 首次收盤獲利的天數
      peak_day             波段最高收盤的天數（= 開始侵蝕的轉折點）
      peak_return          在峰值賣出的報酬%（理想最佳出場）
      run_days             首次獲利 → 峰值 的天數（獲利維持多久才見頂）
      final_return         續抱到觀察窗結束的報酬%（對照：不賣的下場）
      erosion              峰值報酬 − 結束報酬（續抱被侵蝕掉多少%）
    """
    prices = db.query_prices(stock_id)
    if not prices:
        return None
    dates = [p["trade_date"] for p in prices]
    if signal_date not in dates:
        return None
    i0 = dates.index(signal_date)
    forward = prices[i0 + 1:]
    if not forward:
        return None
    entry = forward[0]["open"] or forward[0]["close"]
    if not entry:
        return None

    ws = forward[:window]
    closes = [(j + 1, p["close"]) for j, p in enumerate(ws) if p["close"]]
    if not closes:
        return None

    first_profit_day = None
    for day, px in closes:
        if px > entry:
            first_profit_day = day
            break
    peak_day, peak_px = max(closes, key=lambda t: t[1])
    peak_return = (peak_px / entry - 1) * 100
    final_return = (closes[-1][1] / entry - 1) * 100
    ever_profit = first_profit_day is not None
    run_days = (peak_day - first_profit_day) if ever_profit else None

    return {
        "stock_id": stock_id, "signal_date": signal_date, "entry": entry,
        "ever_profit": ever_profit,
        "days_to_first_profit": first_profit_day,
        "peak_day": peak_day,
        "peak_return": peak_return,
        "run_days": run_days,
        "final_return": final_return,
        "erosion": peak_return - final_return,
    }


def run_path(signal_type="purebuy", window=20):
    db.init_db()
    signals = get_signals(signal_type)
    events = [m for (d, sid, name) in signals
              if (m := path_metrics(sid, d, window)) is not None]

    print(f"=== 路徑分析（動態獲利週期）：訊號 = {signal_type} ===")
    print(f"進場 = 訊號隔日開盤；觀察窗 = {window} 個交易日；原始報酬")
    print(f"可分析事件數：{len(events)}")
    if not events:
        print("⚠️ 無可用事件（請先 update_prices.py）。")
        return
    print()

    n = len(events)
    profitable = [e for e in events if e["ever_profit"]]
    pct_profit = len(profitable) / n * 100

    print(f"【1. 買入後多久會獲利】")
    print(f"  觀察窗內曾獲利的比例：{pct_profit:.0f}%（{len(profitable)}/{n}）")
    print(f"  平均首次獲利天數：{_avg([e['days_to_first_profit'] for e in profitable]):.1f} 天"
          f"（中位數 {_median([e['days_to_first_profit'] for e in profitable]):.0f} 天）")
    print()
    print(f"【2. 開始獲利後多久見頂（開始侵蝕）】")
    print(f"  平均（首次獲利→峰值）：{_avg([e['run_days'] for e in profitable]):.1f} 天"
          f"（中位數 {_median([e['run_days'] for e in profitable]):.0f} 天）")
    print(f"  平均到峰總天數（進場→峰值）：{_avg([e['peak_day'] for e in profitable]):.1f} 天")
    print()
    print(f"【3. 在峰值（侵蝕起點）賣出的獲利】")
    print(f"  平均：{_avg([e['peak_return'] for e in profitable]):+.2f}%"
          f"（中位數 {_median([e['peak_return'] for e in profitable]):+.2f}%）")
    print()
    print(f"【對照：若不賣、續抱到第 {window} 天】")
    print(f"  平均報酬：{_avg([e['final_return'] for e in events]):+.2f}%"
          f"（中位數 {_median([e['final_return'] for e in events]):+.2f}%）")
    print(f"  平均被侵蝕掉：{_avg([e['erosion'] for e in events]):.2f}%（峰值未賣的代價）")
    print()
    print("⚠️ 注意：峰值是事後最佳出場（含未來資訊），實務無法精準賣在最高點；")
    print("   此為「獲利週期的統計描述」，非可直接執行的策略。真正可執行需用移動停利等規則。")


def _bench_index(benchmark="0050"):
    """date -> {'open':, 'close':} map for the benchmark."""
    rows = db.query_prices(benchmark)
    return {r["trade_date"]: r for r in rows}


def strategy_metrics(stock_id, signal_date, trail_pct, max_window, bench_idx):
    """
    可執行策略模擬：訊號隔日開盤進場，移動停利出場。
      - 追蹤進場後的最高收盤（含進場價為起點）
      - 當收盤 <= 最高點 ×(1 - trail_pct%) 時隔日出場（這裡用當日收盤近似出場價）
      - 或達 max_window 天強制出場
    同時計算同期 0050 報酬以求超額報酬。
    """
    prices = db.query_prices(stock_id)
    if not prices:
        return None
    dates = [p["trade_date"] for p in prices]
    if signal_date not in dates:
        return None
    i0 = dates.index(signal_date)
    forward = prices[i0 + 1:]
    if not forward:
        return None
    entry_row = forward[0]
    entry = entry_row["open"] or entry_row["close"]
    entry_date = entry_row["trade_date"]
    if not entry:
        return None

    ws = forward[:max_window]
    peak = entry
    exit_px = None
    exit_day = len(ws)
    exit_date = ws[-1]["trade_date"]
    for day, p in enumerate(ws, 1):
        c = p["close"]
        if not c:
            continue
        if c > peak:
            peak = c
        # trailing stop
        if c <= peak * (1 - trail_pct / 100.0):
            exit_px = c
            exit_day = day
            exit_date = p["trade_date"]
            break
    if exit_px is None:
        exit_px = ws[-1]["close"] or entry  # held to window end

    ret = (exit_px / entry - 1) * 100

    # benchmark (0050) over same entry_date -> exit_date
    excess = None
    be = bench_idx.get(entry_date)
    bx = bench_idx.get(exit_date)
    if be and bx and (be.get("open") or be.get("close")) and bx.get("close"):
        b_entry = be.get("open") or be.get("close")
        b_ret = (bx["close"] / b_entry - 1) * 100
        excess = ret - b_ret

    return {"stock_id": stock_id, "signal_date": signal_date,
            "ret": ret, "exit_day": exit_day, "excess": excess}


def run_strategy(signal_type="purebuy", trail_pct=8.0, max_window=60, benchmark="0050"):
    db.init_db()
    signals = get_signals(signal_type)
    bench_idx = _bench_index(benchmark)
    events = [m for (d, sid, name) in signals
              if (m := strategy_metrics(sid, d, trail_pct, max_window, bench_idx)) is not None]

    print(f"=== 可執行策略回測：訊號 = {signal_type} ===")
    print(f"進場=訊號隔日開盤；移動停利={trail_pct}%；最長持有={max_window}天；基準={benchmark}")
    print(f"可分析事件數：{len(events)}")
    if not events:
        print("⚠️ 無可用事件。"); return
    print()

    rets = [e["ret"] for e in events]
    win = sum(1 for r in rets if r > 0) / len(rets) * 100
    hold = [e["exit_day"] for e in events]
    excess = [e["excess"] for e in events if e["excess"] is not None]
    beat = sum(1 for x in excess if x > 0) / len(excess) * 100 if excess else None

    print(f"【策略績效】")
    print(f"  平均報酬：{_avg(rets):+.2f}%（中位數 {_median(rets):+.2f}%）")
    print(f"  勝率：{win:.0f}%")
    print(f"  平均持有天數：{_avg(hold):.1f} 天（中位數 {_median(hold):.0f} 天）")
    print()
    print(f"【超額報酬 vs {benchmark}】（判定是否真有 edge）")
    if excess:
        print(f"  平均超額報酬：{_avg(excess):+.2f}%（中位數 {_median(excess):+.2f}%）")
        print(f"  贏過大盤比例：{beat:.0f}%")
        verdict = ("✅ 平均超額為正且過半贏大盤 → 訊號可能有 edge"
                   if _avg(excess) > 0 and beat and beat > 50
                   else "⚠️ 超額接近零或勝率不足 → 訊號 edge 不明顯，多為大盤beta")
        print(f"  研判：{verdict}")
    else:
        print(f"  （無 {benchmark} 對應股價，無法計算）")
    print()
    print("註：出場以觸發當日收盤近似（實務略有滑點）；報酬為原始，超額已扣基準。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", default="purebuy",
                    choices=["purebuy", "strongbuy", "topnet"])
    ap.add_argument("--mode", default="window",
                    choices=["window", "path", "strategy"],
                    help="window=固定持有; path=動態週期; strategy=移動停利+超額")
    ap.add_argument("--windows", default="1,3,5,10,20", help="window 模式用")
    ap.add_argument("--window", type=int, default=20, help="path 模式觀察窗天數")
    ap.add_argument("--trail", type=float, default=8.0, help="strategy 移動停利%")
    ap.add_argument("--max-window", type=int, default=60, help="strategy 最長持有天數")
    ap.add_argument("--benchmark", default="0050", help="strategy 超額報酬基準")
    args = ap.parse_args()
    if args.mode == "path":
        run_path(signal_type=args.signal, window=args.window)
    elif args.mode == "strategy":
        run_strategy(signal_type=args.signal, trail_pct=args.trail,
                     max_window=args.max_window, benchmark=args.benchmark)
    else:
        windows = tuple(int(x) for x in args.windows.split(","))
        run(signal_type=args.signal, windows=windows)
