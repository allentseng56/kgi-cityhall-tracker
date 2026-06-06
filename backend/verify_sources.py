# -*- coding: utf-8 -*-
"""
驗證工具：以官方 TWSE BSR 為基準，交叉比對富邦 DJ 的分點資料。

TWSE BSR 是政府官方資料、與富邦(嘉實)後台完全獨立，是最權威的驗證基準。
我們資料庫裡「有價格」(avg_buy_price IS NOT NULL) 的列來自官方 BSR；
本工具對這些列逐筆抓富邦同日數字比對，計算吻合率。

比對方式：富邦以「張」為單位（四捨五入），故將 BSR 股數轉成張後比對：
    round(BSR_買股數 / 1000) == 富邦_買張   且   賣方亦同

用法：
    python verify_sources.py                # 驗證所有 BSR 來源列
    python verify_sources.py --date 2026-05-15
    python verify_sources.py --tolerance 1  # 允許 ±1 張誤差（捨入邊界）
"""
import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from fubon_fetcher import fetch_branch_series


def bsr_rows(date_filter=None):
    """Return BSR-sourced rows (avg_buy_price not null) optionally for one date."""
    q = ("SELECT trade_date, stock_id, stock_name, buy_shares, sell_shares "
         "FROM kgi_cityhall_daily WHERE avg_buy_price IS NOT NULL")
    args = []
    if date_filter:
        q += " AND trade_date=?"; args.append(date_filter)
    with db.get_conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def run(date_filter=None, tolerance=1, pause=1.5):
    rows = bsr_rows(date_filter)
    if not rows:
        print("沒有 BSR 來源資料可驗證（avg_buy_price 皆為空）。")
        return

    # group by stock to minimise requests (one fubon call per stock covers all its dates)
    by_stock = defaultdict(list)
    for r in rows:
        by_stock[r["stock_id"]].append(r)
    dates = sorted({r["trade_date"] for r in rows})
    start, end = dates[0], dates[-1]

    print(f"驗證基準：官方 TWSE BSR（獨立來源）")
    print(f"比對對象：富邦 DJ")
    print(f"樣本：{len(rows)} 筆 / {len(by_stock)} 檔 / 日期 {start}~{end}")
    print(f"容許誤差：±{tolerance} 張（四捨五入邊界）\n")

    match = 0
    mismatch = 0
    nofubon = 0
    mismatches = []

    for i, (sid, srows) in enumerate(sorted(by_stock.items()), 1):
        try:
            series = fetch_branch_series(sid, start, end)
        except Exception as e:
            print(f"  [{i}] {sid}: 富邦抓取失敗 {e}")
            nofubon += len(srows)
            time.sleep(pause)
            continue
        fubon_by_date = {s["trade_date"]: s for s in series}
        for r in srows:
            f = fubon_by_date.get(r["trade_date"])
            bsr_buy_lots = round(r["buy_shares"] / 1000)
            bsr_sell_lots = round(r["sell_shares"] / 1000)
            if f is None:
                nofubon += 1
                mismatches.append((r, None))
                continue
            buy_ok = abs(f["buy_lots"] - bsr_buy_lots) <= tolerance
            sell_ok = abs(f["sell_lots"] - bsr_sell_lots) <= tolerance
            if buy_ok and sell_ok:
                match += 1
            else:
                mismatch += 1
                mismatches.append((r, f))
        time.sleep(pause)

    total = match + mismatch + nofubon
    print(f"\n=== 驗證結果 ===")
    print(f"  完全吻合：{match} / {total}  ({match/total*100:.1f}%)")
    print(f"  不吻合：  {mismatch}")
    print(f"  富邦無此日資料：{nofubon}")

    if mismatches:
        print(f"\n=== 不吻合明細（前 20 筆）===")
        print(f"{'日期':<12}{'股票':<14}{'BSR買/賣(張)':>16}{'富邦買/賣(張)':>16}")
        for r, f in mismatches[:20]:
            bsr = f"{round(r['buy_shares']/1000)}/{round(r['sell_shares']/1000)}"
            fub = f"{f['buy_lots']}/{f['sell_lots']}" if f else "(無)"
            print(f"{r['trade_date']:<12}{r['stock_id']+' '+r['stock_name']:<14}{bsr:>16}{fub:>16}")

    print("\n判讀：")
    if total and match / total >= 0.95:
        print("  ✅ 吻合率 ≥95%，富邦 DJ 資料高度可信，可用於回測。")
    elif total and match / total >= 0.85:
        print("  ⚠️ 吻合率 85-95%，多數可信但有少數差異，建議檢視不吻合明細。")
    else:
        print("  ❌ 吻合率 <85%，差異偏大，回測前需釐清來源差異。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--tolerance", type=int, default=1)
    ap.add_argument("--pause", type=float, default=1.5)
    args = ap.parse_args()
    run(date_filter=args.date, tolerance=args.tolerance, pause=args.pause)
