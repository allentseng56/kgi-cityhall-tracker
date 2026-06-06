# -*- coding: utf-8 -*-
"""Fetch market-wide top-N stocks by trading volume from TWSE OpenAPI."""
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

# OpenAPI: 所有上市股票最新交易日成交資訊
# Each record:
#   Date(民國YYY-MM-DD as 7-digit string), Code, Name, TradeVolume, TradeValue,
#   OpeningPrice, HighestPrice, LowestPrice, ClosingPrice, Change, Transaction
TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"


def _roc_to_iso(roc):
    """'1150515' -> '2026-05-15'"""
    if not roc or len(roc) < 7:
        return None
    yr = int(roc[:3]) + 1911
    return f"{yr:04d}-{roc[3:5]}-{roc[5:7]}"


def _to_int(s):
    if s is None:
        return 0
    s = str(s).replace(",", "").strip()
    if not s or s in ("--", "X"):
        return 0
    try:
        return int(s)
    except ValueError:
        return 0


def fetch_top_n(n=100, retries=4, pause=3.0):
    """
    Returns (trade_date_iso, list of {rank, stock_id, stock_name, volume}).
    The endpoint always returns the latest trading day's data.

    TWSE OpenAPI intermittently returns a non-JSON error/empty page (esp. from
    non-Taiwan IPs like GitHub Actions). Retry a few times before giving up so
    the caller can decide to fall back to a cached universe.
    """
    import time
    last_err = None
    rows = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(TWSE_URL, headers=HEADERS, timeout=30)
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            if "json" not in ct.lower() and not r.text.lstrip().startswith("["):
                raise RuntimeError(f"non-JSON response (CT={ct!r}, head={r.text[:60]!r})")
            rows = r.json()
            if isinstance(rows, list) and rows:
                break
            raise RuntimeError("empty list")
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(pause)
    if not rows:
        raise RuntimeError(f"TWSE OpenAPI failed after {retries} tries: {last_err}")

    trade_date = _roc_to_iso(rows[0].get("Date", ""))

    parsed = []
    for row in rows:
        sid = (row.get("Code") or "").strip()
        # Keep only pure 4-digit common stocks (filter ETFs, 權證, 特別股 etc.)
        if not (len(sid) == 4 and sid.isdigit()):
            continue
        parsed.append({
            "stock_id": sid,
            "stock_name": (row.get("Name") or "").strip(),
            "volume": _to_int(row.get("TradeVolume")),
        })

    parsed.sort(key=lambda x: x["volume"], reverse=True)
    top = parsed[:n]
    for i, r in enumerate(top, 1):
        r["rank"] = i
    return trade_date, top


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    date, rows = fetch_top_n(n)
    print(f"Trade date: {date}")
    for r in rows:
        print(f"{r['rank']:3d}  {r['stock_id']}  {r['stock_name']:<12s}  {r['volume']:>15,}")
