# -*- coding: utf-8 -*-
"""
Update price_history for every stock that has ever appeared as a 凱基市府
signal. Fetches from the earliest signal date minus a buffer, through today
plus the forward window we need for event-study (default +30 calendar days
auto-extends as days pass since we re-run regularly).

Usage:
    python update_prices.py                 # update all signal stocks
    python update_prices.py --since 2024-01-01
    python update_prices.py --stock 2330
"""
import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from price_fetcher import fetch_price_history

# Lookback buffer before earliest signal (so pre-signal baseline exists)
PRE_BUFFER_DAYS = 30
# Pause between FinMind calls to respect rate limit (~300/hr free => >12s safe,
# but in practice 600/hr with token => 6s; default 4s works for <100 stocks).
DEFAULT_PAUSE = 4.0


def run(since=None, only_stock=None, pause=DEFAULT_PAUSE, token=None):
    db.init_db()

    if only_stock:
        stocks = [only_stock]
    else:
        stocks = db.signal_stock_ids()
    if not stocks:
        print("No signal stocks in DB yet — run run_daily.py first.")
        return

    # Determine global start date
    if since:
        start_date = since
    else:
        earliest = db.earliest_signal_date()
        if not earliest:
            start_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        else:
            d = datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=PRE_BUFFER_DAYS)
            start_date = d.strftime("%Y-%m-%d")

    end_date = datetime.now().strftime("%Y-%m-%d")
    print(f"Updating prices for {len(stocks)} stock(s), {start_date} ~ {end_date}")

    ok = 0
    fail = 0
    for i, sid in enumerate(stocks, 1):
        try:
            rows = fetch_price_history(sid, start_date, end_date, token=token)
            db.upsert_prices(sid, rows)
            ok += 1
            print(f"  [{i}/{len(stocks)}] {sid}: {len(rows)} rows")
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(stocks)}] {sid}: FAIL {e}")
        time.sleep(pause)

    cov = db.price_coverage()
    print(f"\nDone. ok={ok} fail={fail}. "
          f"price_history now: {cov['stocks']} stocks / {cov['rows']} rows")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="override start date YYYY-MM-DD")
    ap.add_argument("--stock", help="update only this stock_id")
    ap.add_argument("--pause", type=float, default=DEFAULT_PAUSE)
    ap.add_argument("--token", help="FinMind API token (or set FINMIND_TOKEN env)")
    args = ap.parse_args()
    run(since=args.since, only_stock=args.stock, pause=args.pause, token=args.token)
