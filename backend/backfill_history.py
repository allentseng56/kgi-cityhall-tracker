# -*- coding: utf-8 -*-
"""
回補凱基市府歷史分點資料（來源：富邦 DJ，免費）。

對 daily_top100 名單中的每檔股票，抓取凱基市府自 SINCE 起的每日買賣，
轉成股數（張 × 1000）寫入 kgi_cityhall_daily。

- 不覆寫既有資料（INSERT OR IGNORE）：保護精確的官方 BSR 列。
- 回補列無價格/金額（NULL），可藉此辨識來源。
- 富邦單一請求即可回傳整段日期區間（已驗證 3 年一次到位）。

用法：
    python backfill_history.py                  # Top100, 自 2023-01-01
    python backfill_history.py --since 2025-01-01
    python backfill_history.py --stock 2330     # 只回補單檔（測試）
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from fubon_fetcher import fetch_branch_series

DEFAULT_SINCE = "2023-01-01"
LOTS_TO_SHARES = 1000
DEFAULT_PAUSE = 2.0


def run(since=DEFAULT_SINCE, only_stock=None, pause=DEFAULT_PAUSE):
    db.init_db()
    end = datetime.now().strftime("%Y-%m-%d")

    if only_stock:
        # need a name; pull from top100 if present else blank
        names = dict(db.top100_list())
        stocks = [(only_stock, names.get(only_stock, ""))]
    else:
        stocks = db.top100_list()
    if not stocks:
        print("daily_top100 為空 — 請先跑 run_daily.py 產生 Top100 名單。")
        return

    print(f"回補凱基市府歷史：{len(stocks)} 檔，{since} ~ {end}")
    print(f"來源：富邦 DJ（張×1000=股，不覆寫既有 BSR 列）\n")

    tot_inserted = 0
    tot_skipped = 0
    ok = fail = 0
    for i, (sid, name) in enumerate(stocks, 1):
        try:
            series = fetch_branch_series(sid, since, end)
            inserted = skipped = 0
            for row in series:
                if row["buy_lots"] == 0 and row["sell_lots"] == 0:
                    continue
                did = db.insert_kgi_if_absent(
                    row["trade_date"], sid, name,
                    row["buy_lots"] * LOTS_TO_SHARES,
                    row["sell_lots"] * LOTS_TO_SHARES,
                )
                if did:
                    inserted += 1
                else:
                    skipped += 1
            tot_inserted += inserted
            tot_skipped += skipped
            ok += 1
            print(f"  [{i}/{len(stocks)}] {sid} {name}: "
                  f"{len(series)} 天，新增 {inserted}，已存在 {skipped}")
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(stocks)}] {sid} {name}: FAIL {e}")
        time.sleep(pause)

    cov = db.query_coverage()
    print(f"\n完成。成功 {ok} / 失敗 {fail}。")
    print(f"本次新增 {tot_inserted} 列，跳過（已存在）{tot_skipped} 列。")
    print(f"kgi_cityhall_daily 現況：{cov['days']} 個交易日 / {cov['rows']} 列")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=DEFAULT_SINCE)
    ap.add_argument("--stock")
    ap.add_argument("--pause", type=float, default=DEFAULT_PAUSE)
    args = ap.parse_args()
    run(since=args.since, only_stock=args.stock, pause=args.pause)
