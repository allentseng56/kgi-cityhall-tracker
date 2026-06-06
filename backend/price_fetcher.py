# -*- coding: utf-8 -*-
"""
Price history fetcher using FinMind free-tier TaiwanStockPrice dataset.

FinMind free tier:
  - TaiwanStockPrice IS available for free (verified).
  - Rate limit: ~300 requests/hour anonymous (600/hr with a free API token).
  - One request returns a full date range for one stock, so 100 stocks = 100 req.

If a FINMIND_TOKEN env var is set, it's sent for the higher rate limit.
"""
import os
import time
import requests

API = "https://api.finmindtrade.com/api/v4/data"


def fetch_price_history(stock_id, start_date, end_date=None, token=None, timeout=30):
    """
    Returns list of dicts {trade_date, open, high, low, close, volume}.
    Raises RuntimeError on API-level failure.
    """
    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": stock_id,
        "start_date": start_date,
    }
    if end_date:
        params["end_date"] = end_date
    token = token or os.environ.get("FINMIND_TOKEN")
    if token:
        params["token"] = token

    r = requests.get(API, params=params, timeout=timeout)
    j = r.json()
    if j.get("msg") != "success":
        raise RuntimeError(f"FinMind error for {stock_id}: {j.get('msg')!r}")
    out = []
    for row in j.get("data", []):
        out.append({
            "trade_date": row["date"],
            "open": row.get("open"),
            "high": row.get("max"),
            "low": row.get("min"),
            "close": row.get("close"),
            "volume": row.get("Trading_Volume"),
        })
    return out


if __name__ == "__main__":
    import sys
    sid = sys.argv[1] if len(sys.argv) > 1 else "2330"
    start = sys.argv[2] if len(sys.argv) > 2 else "2025-05-01"
    rows = fetch_price_history(sid, start)
    print(f"{sid}: {len(rows)} rows from {start}")
    for r in rows[:5]:
        print(f"  {r['trade_date']}  O={r['open']} H={r['high']} "
              f"L={r['low']} C={r['close']} V={r['volume']:,}")
