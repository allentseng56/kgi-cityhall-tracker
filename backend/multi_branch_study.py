# -*- coding: utf-8 -*-
"""
測試台灣前三大「本土零售分點」(隔日沖主力) 的買訊有無 edge。

分點（富邦 b-code，從排行頁取得）：
  1. 犇亞-鑫豐   0036003000310064
  2. 凱基-台北   9268
  3. 永豐金-匯立 0039004100380031

流程：
  backfill : 對 Top100 universe 回補各分點 2 年歷史 → branch_daily
  analyze  : 對每分點抽買訊，用移動停利策略 + 對 0050 超額報酬評估 edge
用法：
  python multi_branch_study.py backfill
  python multi_branch_study.py analyze [--trail 8] [--signal purebuy]
"""
import argparse
import math
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from fubon_fetcher import fetch_branch_series
from event_study import strategy_metrics, _bench_index, _avg, _median

# 來回交易成本：買手續費0.1425% + 賣手續費0.1425% + 證交稅0.3% ≈ 0.585%
ROUND_TRIP_COST = 0.585

BRANCHES = [
    ("犇亞-鑫豐", "0036003000310064"),
    ("凱基-台北", "9268"),
    ("永豐金-匯立", "0039004100380031"),
]
SINCE = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
LOTS = 1000


def backfill(pause=1.2, since=SINCE):
    db.init_db()
    end = datetime.now().strftime("%Y-%m-%d")
    stocks = db.top100_list()
    print(f"回補 {len(BRANCHES)} 分點 × {len(stocks)} 檔，{since}~{end}\n")
    for name, b in BRANCHES:
        ins = 0
        for i, (sid, sname) in enumerate(stocks, 1):
            try:
                series = fetch_branch_series(sid, since, end, b_raw=b)
                for r in series:
                    if r["buy_lots"] or r["sell_lots"]:
                        if db.insert_branch_if_absent(b, name, r["trade_date"], sid, sname,
                                                       r["buy_lots"]*LOTS, r["sell_lots"]*LOTS):
                            ins += 1
            except Exception as e:
                print(f"  {name} {sid}: {e}")
            time.sleep(pause)
        cov = db.branch_coverage(b)
        print(f"  ✓ {name}: 新增 {ins} 列，累計 {cov['days']} 天 / {cov['rows']} 列 / "
              f"{cov['turnover']/1000:,.0f} 張")


def analyze(signal_type="purebuy", trail=8.0, max_window=60, benchmark="0050"):
    db.init_db()
    bench_idx = _bench_index(benchmark)
    print(f"=== 三大本土零售分點 edge 測試 ===")
    print(f"訊號={signal_type}；進場=隔日開盤；移動停利={trail}%；最長{max_window}天；基準={benchmark}\n")
    print(f"{'分點':<14}{'事件':>6}{'平均報酬':>9}{'中位':>8}{'勝率':>6}{'平均超額':>9}{'贏大盤':>7}  研判")
    for name, b in BRANCHES:
        signals = db.query_branch_signals(b, signal_type, since=SINCE)
        events = [m for (d, sid, _n) in signals
                  if (m := strategy_metrics(sid, d, trail, max_window, bench_idx)) is not None]
        if not events:
            print(f"{name:<14}{'無資料':>6}")
            continue
        rets = [e["ret"] for e in events]
        win = sum(1 for r in rets if r > 0) / len(rets) * 100
        ex = [e["excess"] for e in events if e["excess"] is not None]
        beat = (sum(1 for x in ex if x > 0) / len(ex) * 100) if ex else 0
        avg_ex = _avg(ex) if ex else None
        verdict = "✅有edge" if (avg_ex and avg_ex > 0.5 and beat > 50) else "❌無edge"
        print(f"{name:<14}{len(events):>6}{_avg(rets):>+8.2f}%{_median(rets):>+7.2f}%"
              f"{win:>5.0f}%{(avg_ex if avg_ex else 0):>+8.2f}%{beat:>6.0f}%  {verdict}")
    print("\n註：隔日沖分點『買進』常為當沖/隔日獲利了結，跟單其買訊未必有利，本表即在驗證。")


def extreme(trail=8.0, max_window=60, benchmark="0050"):
    """極端金額測試：只取淨買金額前 1%/5%/10% 的事件，看重壓買進有無 edge。"""
    db.init_db()
    bench_idx = _bench_index(benchmark)
    print(f"=== 極端金額門檻測試（淨買金額最大的事件）===")
    print(f"進場=隔日開盤；移動停利={trail}%；最長{max_window}天；基準={benchmark}\n")
    for name, b in BRANCHES:
        evs = db.query_branch_net_buys(b, since=SINCE)
        if not evs:
            print(f"{name}: 無資料"); continue
        evs.sort(key=lambda x: x[3], reverse=True)  # 依淨買金額降序
        print(f"【{name}】淨買事件 {len(evs)} 筆")
        print(f"  {'門檻':<10}{'事件':>6}{'金額下限':>12}{'平均報酬':>9}{'中位':>8}{'勝率':>6}{'超額':>8}{'贏大盤':>7}  研判")
        for pct in (10, 5, 1):
            k = max(1, int(len(evs) * pct / 100))
            subset = evs[:k]
            floor_amt = subset[-1][3]
            events = [m for (d, sid, _n, _a) in subset
                      if (m := strategy_metrics(sid, d, trail, max_window, bench_idx)) is not None]
            if not events:
                print(f"  前{pct}%      無可分析"); continue
            rets = [e["ret"] for e in events]
            win = sum(1 for r in rets if r > 0) / len(rets) * 100
            ex = [e["excess"] for e in events if e["excess"] is not None]
            beat = (sum(1 for x in ex if x > 0)/len(ex)*100) if ex else 0
            avg_ex = _avg(ex) if ex else 0
            verdict = "✅有edge" if (avg_ex and avg_ex>0.5 and beat>50) else "❌無edge"
            print(f"  前{pct}%{'':<6}{len(events):>6}{floor_amt/1e8:>10.2f}億"
                  f"{_avg(rets):>+8.2f}%{_median(rets):>+7.2f}%{win:>5.0f}%"
                  f"{avg_ex:>+7.2f}%{beat:>6.0f}%  {verdict}")
        print()


def _pooled_net_buys(since=None):
    """合併三分點淨買事件，(日期,股票)去重取最大金額。回傳 [(date, sid, amount)]。"""
    pooled = {}
    for name, b in BRANCHES:
        for (d, sid, _n, amt) in db.query_branch_net_buys(b, since=since):
            key = (d, sid)
            if key not in pooled or amt > pooled[key]:
                pooled[key] = amt
    return [(d, sid, amt) for (d, sid), amt in pooled.items()]


def _eval(subset, trail, max_window, bench_idx):
    """對 [(date,sid)] 計算扣成本後統計 + t檢定。回傳 dict 或 None。"""
    events = [m for (d, sid) in subset
              if (m := strategy_metrics(sid, d, trail, max_window, bench_idx)) is not None]
    ex = [e["excess"] - ROUND_TRIP_COST for e in events if e["excess"] is not None]
    rets = [e["ret"] - ROUND_TRIP_COST for e in events]
    if len(ex) < 5:
        return None
    sd = statistics.stdev(ex) if len(ex) > 1 else 0
    avg_ex = statistics.mean(ex)
    t = avg_ex / (sd / math.sqrt(len(ex))) if sd else 0
    return {
        "n": len(rets), "net_ret": statistics.mean(rets),
        "median": statistics.median(rets),
        "win": sum(1 for r in rets if r > 0) / len(rets) * 100,
        "net_excess": avg_ex,
        "beat": sum(1 for x in ex if x > 0) / len(ex) * 100,
        "t": t,
        "sig": "***p<.01" if abs(t) > 2.58 else ("**p<.05" if abs(t) > 1.96 else "不顯著"),
    }


def oos(cutoff="2024-12-31", trail=8.0, max_window=60, benchmark="0050"):
    """樣本外驗證：cutoff 前=訓練(in-sample)，cutoff 後=測試(out-of-sample)。
    同一條規則(門檻+移動停利)在兩段是否都成立。"""
    db.init_db()
    bench_idx = _bench_index(benchmark)
    allev = _pooled_net_buys(since=None)  # 用全部歷史
    train = [(d, sid, a) for (d, sid, a) in allev if d <= cutoff]
    test = [(d, sid, a) for (d, sid, a) in allev if d > cutoff]
    print(f"=== 樣本外驗證（OOS）===")
    print(f"切點={cutoff}；移動停利={trail}%；成本={ROUND_TRIP_COST}%；基準={benchmark}")
    print(f"訓練事件池 {len(train):,}（≤{cutoff}）／測試事件池 {len(test):,}（>{cutoff}）\n")
    for thr_e in (1, 3, 5):
        thr = thr_e * 1e8
        print(f"── 門檻 ≥{thr_e}億 ──")
        for label, pool in (("訓練(in-sample)", train), ("測試(out-of-sample)", test)):
            subset = [(d, sid) for (d, sid, a) in pool if a >= thr]
            r = _eval(subset, trail, max_window, bench_idx)
            if not r:
                print(f"  {label:<22} 樣本不足"); continue
            edge = "✅" if (r["net_excess"] > 0 and abs(r["t"]) > 1.96) else "❌"
            print(f"  {label:<22} n={r['n']:>4} 淨報酬{r['net_ret']:>+6.2f}% "
                  f"中位{r['median']:>+6.2f}% 勝率{r['win']:>3.0f}% "
                  f"淨超額{r['net_excess']:>+6.2f}% t={r['t']:>5.2f} {r['sig']:<8} {edge}")
        print()
    print("判讀：若『測試(out-of-sample)』仍 ✅(淨超額>0且顯著)，代表 edge 非過度配適，較可信。")


def validate(trail=8.0, max_window=60, benchmark="0050"):
    """
    驗證極端金額 edge：①合併三分點擴大樣本 ②計入交易成本 ③t檢定顯著性。
    訊號：任一分點對某股「極端淨買金額」≥ 門檻；進場隔日開盤、移動停利。
    """
    db.init_db()
    bench_idx = _bench_index(benchmark)

    # ① 合併三分點淨買事件，依 (日期,股票) 去重取最大金額（避免重複計算同一結果）
    pooled = {}
    for name, b in BRANCHES:
        for (d, sid, _n, amt) in db.query_branch_net_buys(b, since=SINCE):
            key = (d, sid)
            if key not in pooled or amt > pooled[key]:
                pooled[key] = amt
    events_all = [(d, sid, amt) for (d, sid), amt in pooled.items()]
    print(f"=== 極端金額 edge 驗證（三分點合併、計入成本、t檢定）===")
    print(f"進場=隔日開盤；移動停利={trail}%；最長{max_window}天；基準={benchmark}")
    print(f"來回交易成本={ROUND_TRIP_COST}%；合併去重後淨買事件 {len(events_all):,} 筆\n")

    print(f"{'金額門檻':>8}{'事件':>6}{'淨報酬':>9}{'中位':>8}{'勝率':>6}"
          f"{'淨超額':>9}{'贏大盤':>7}{'t值':>7}{'顯著':>8}  研判")
    for thr_e in (0.5, 1, 3, 5, 10):  # 億元
        thr = thr_e * 1e8
        subset = [(d, sid) for (d, sid, amt) in events_all if amt >= thr]
        events = [m for (d, sid) in subset
                  if (m := strategy_metrics(sid, d, trail, max_window, bench_idx)) is not None]
        ex = [e["excess"] - ROUND_TRIP_COST for e in events if e["excess"] is not None]
        rets = [e["ret"] - ROUND_TRIP_COST for e in events]
        if len(ex) < 5:
            print(f"{thr_e:>6.1f}億{len(events):>6}   樣本不足")
            continue
        win = sum(1 for r in rets if r > 0) / len(rets) * 100
        beat = sum(1 for x in ex if x > 0) / len(ex) * 100
        avg_ex = statistics.mean(ex)
        sd = statistics.stdev(ex) if len(ex) > 1 else 0
        t = avg_ex / (sd / math.sqrt(len(ex))) if sd else 0
        sig = "***p<.01" if abs(t) > 2.58 else ("**p<.05" if abs(t) > 1.96 else "不顯著")
        edge = "✅有edge" if (avg_ex > 0 and abs(t) > 1.96) else "❌"
        print(f"{thr_e:>6.1f}億{len(events):>6}{statistics.mean(rets):>+8.2f}%"
              f"{statistics.median(rets):>+7.2f}%{win:>5.0f}%{avg_ex:>+8.2f}%"
              f"{beat:>6.0f}%{t:>7.2f}{sig:>9}  {edge}")
    print("\n判讀：t檢定 H0=平均淨超額為0。|t|>1.96(p<.05)且淨超額>0 → 統計上有edge。")
    print("注：成本含買賣手續費+證交稅(來回0.585%)；超額已扣此成本(保守，視0050為被動持有)。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "analyze", "extreme", "validate", "oos"])
    ap.add_argument("--since", default=SINCE, help="backfill 起始日")
    ap.add_argument("--cutoff", default="2024-12-31", help="oos 訓練/測試切點")
    ap.add_argument("--signal", default="purebuy",
                    choices=["purebuy", "strongbuy", "topnet",
                             "puresell", "strongsell", "topnetsell"])
    ap.add_argument("--trail", type=float, default=8.0)
    args = ap.parse_args()
    if args.cmd == "backfill":
        backfill(since=args.since)
    elif args.cmd == "extreme":
        extreme(trail=args.trail)
    elif args.cmd == "validate":
        validate(trail=args.trail)
    elif args.cmd == "oos":
        oos(cutoff=args.cutoff, trail=args.trail)
    else:
        analyze(signal_type=args.signal, trail=args.trail)
