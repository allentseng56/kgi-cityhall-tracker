# -*- coding: utf-8 -*-
"""SQLite schema + query helpers for KGI City-Hall Tracker."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tracker.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_top100 (
    trade_date TEXT NOT NULL,
    rank INTEGER NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    volume INTEGER NOT NULL,
    PRIMARY KEY (trade_date, stock_id)
);

CREATE TABLE IF NOT EXISTS kgi_cityhall_daily (
    trade_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    buy_shares INTEGER NOT NULL,
    sell_shares INTEGER NOT NULL,
    avg_buy_price REAL,
    avg_sell_price REAL,
    buy_amount INTEGER,
    sell_amount INTEGER,
    net_shares INTEGER,
    turnover INTEGER,
    PRIMARY KEY (trade_date, stock_id)
);

CREATE TABLE IF NOT EXISTS fetch_errors (
    trade_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    error_msg TEXT,
    attempted_at TEXT,
    PRIMARY KEY (trade_date, stock_id)
);

-- 股價歷史（FinMind 免費版 TaiwanStockPrice），用於事件研究/回測
CREATE TABLE IF NOT EXISTS price_history (
    stock_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,        -- YYYY-MM-DD
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,                  -- 成交股數
    PRIMARY KEY (stock_id, trade_date)
);

-- 通用多分點每日資料（測試其他券商分點用，來源：富邦DJ，張×1000=股）
CREATE TABLE IF NOT EXISTS branch_daily (
    broker_code TEXT NOT NULL,       -- 富邦 b-code（識別分點）
    broker_name TEXT NOT NULL,       -- 如 凱基-台北
    trade_date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    buy_shares INTEGER NOT NULL,
    sell_shares INTEGER NOT NULL,
    net_shares INTEGER,
    turnover INTEGER,
    PRIMARY KEY (broker_code, trade_date, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_branch ON branch_daily(broker_code, trade_date);
CREATE INDEX IF NOT EXISTS idx_kgi_date ON kgi_cityhall_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_kgi_stock ON kgi_cityhall_daily(stock_id);
CREATE INDEX IF NOT EXISTS idx_price_stock ON price_history(stock_id);
CREATE INDEX IF NOT EXISTS idx_price_date ON price_history(trade_date);
"""


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as c:
        c.executescript(SCHEMA)


def upsert_top100(trade_date, rows):
    """rows: list of dict {rank, stock_id, stock_name, volume}"""
    with get_conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO daily_top100
               (trade_date, rank, stock_id, stock_name, volume)
               VALUES (?, ?, ?, ?, ?)""",
            [(trade_date, r["rank"], r["stock_id"], r["stock_name"], r["volume"]) for r in rows],
        )


def upsert_kgi_row(trade_date, stock_id, stock_name, agg):
    """agg: dict with buy_shares, sell_shares, avg_buy_price, avg_sell_price"""
    bs = agg["buy_shares"]
    ss = agg["sell_shares"]
    bp = agg.get("avg_buy_price")
    sp = agg.get("avg_sell_price")
    buy_amt = int(bs * bp) if bp else 0
    sell_amt = int(ss * sp) if sp else 0
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO kgi_cityhall_daily
               (trade_date, stock_id, stock_name, buy_shares, sell_shares,
                avg_buy_price, avg_sell_price, buy_amount, sell_amount,
                net_shares, turnover)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_date, stock_id, stock_name, bs, ss, bp, sp,
             buy_amt, sell_amt, bs - ss, bs + ss),
        )


def insert_kgi_if_absent(trade_date, stock_id, stock_name, buy_shares, sell_shares):
    """
    Insert a historical (backfilled) row only if (trade_date, stock_id) doesn't
    already exist — protects exact BSR rows from being overwritten by rounded
    富邦 data. Backfilled rows have NULL prices/amounts (implicit source marker).
    Returns True if inserted, False if a row already existed.
    """
    with get_conn() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO kgi_cityhall_daily
               (trade_date, stock_id, stock_name, buy_shares, sell_shares,
                avg_buy_price, avg_sell_price, buy_amount, sell_amount,
                net_shares, turnover)
               VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)""",
            (trade_date, stock_id, stock_name, buy_shares, sell_shares,
             buy_shares - sell_shares, buy_shares + sell_shares),
        )
        return cur.rowcount > 0


def insert_branch_if_absent(broker_code, broker_name, trade_date, stock_id,
                            stock_name, buy_shares, sell_shares):
    with get_conn() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO branch_daily
               (broker_code, broker_name, trade_date, stock_id, stock_name,
                buy_shares, sell_shares, net_shares, turnover)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (broker_code, broker_name, trade_date, stock_id, stock_name,
             buy_shares, sell_shares, buy_shares - sell_shares, buy_shares + sell_shares),
        )
        return cur.rowcount > 0


def query_branch_signals(broker_code, signal_type="purebuy", min_shares=10000, since=None):
    """Return [(trade_date, stock_id, stock_name)] qualifying buy signals for a branch."""
    q = "SELECT trade_date, stock_id, stock_name, buy_shares, sell_shares, net_shares FROM branch_daily WHERE broker_code=?"
    args = [broker_code]
    if since:
        q += " AND trade_date >= ?"; args.append(since)
    with get_conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    sig = []
    for r in rows:
        if signal_type == "purebuy":
            ok = r["sell_shares"] == 0 and r["buy_shares"] >= min_shares
        elif signal_type == "strongbuy":
            ok = r["buy_shares"] >= 5 * max(r["sell_shares"], 1) and r["net_shares"] >= min_shares
        elif signal_type == "topnet":
            ok = r["net_shares"] >= min_shares
        # 賣超訊號（反指標測試：分點賣超 → 隔日買進）
        elif signal_type == "puresell":
            ok = r["buy_shares"] == 0 and r["sell_shares"] >= min_shares
        elif signal_type == "strongsell":
            ok = r["sell_shares"] >= 5 * max(r["buy_shares"], 1) and -r["net_shares"] >= min_shares
        elif signal_type == "topnetsell":
            ok = -r["net_shares"] >= min_shares
        else:
            ok = False
        if ok:
            sig.append((r["trade_date"], r["stock_id"], r["stock_name"]))
    return sig


def query_branch_net_buys(broker_code, since=None):
    """淨買事件 [(date, stock_id, name, net_buy_amount)]，金額=淨買股數×當日收盤。
    用於『極端金額』篩選（只看買方，net_shares>0）。"""
    q = ("SELECT b.trade_date, b.stock_id, b.stock_name, b.net_shares, p.close "
         "FROM branch_daily b LEFT JOIN price_history p "
         "ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date "
         "WHERE b.broker_code=? AND b.net_shares>0")
    args = [broker_code]
    if since:
        q += " AND b.trade_date>=?"; args.append(since)
    out = []
    with get_conn() as c:
        for r in c.execute(q, args).fetchall():
            if r["close"]:
                out.append((r["trade_date"], r["stock_id"], r["stock_name"],
                            r["net_shares"] * r["close"]))
    return out


def branch_coverage(broker_code):
    with get_conn() as c:
        row = c.execute("SELECT COUNT(DISTINCT trade_date), COUNT(*), SUM(turnover) "
                        "FROM branch_daily WHERE broker_code=?", (broker_code,)).fetchone()
    return {"days": row[0], "rows": row[1], "turnover": row[2] or 0}


def top100_list():
    """Return [(stock_id, stock_name)] from the most recent daily_top100 snapshot."""
    with get_conn() as c:
        latest = c.execute("SELECT MAX(trade_date) FROM daily_top100").fetchone()[0]
        if not latest:
            return []
        return [(r["stock_id"], r["stock_name"]) for r in c.execute(
            "SELECT stock_id, stock_name FROM daily_top100 WHERE trade_date=? ORDER BY rank",
            (latest,)).fetchall()]


def log_error(trade_date, stock_id, msg):
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO fetch_errors
               (trade_date, stock_id, error_msg, attempted_at)
               VALUES (?, ?, ?, ?)""",
            (trade_date, stock_id, msg, datetime.now().isoformat(timespec="seconds")),
        )


def clear_error(trade_date, stock_id):
    with get_conn() as c:
        c.execute(
            "DELETE FROM fetch_errors WHERE trade_date=? AND stock_id=?",
            (trade_date, stock_id),
        )


def query_top10_recent(days=7):
    """Top 10 stocks by turnover for the most recent N trading days present in DB."""
    cutoff = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    with get_conn() as c:
        # get the distinct trade_dates we have, take the last N
        dates = [r[0] for r in c.execute(
            "SELECT DISTINCT trade_date FROM kgi_cityhall_daily WHERE trade_date >= ? "
            "ORDER BY trade_date DESC LIMIT ?", (cutoff, days)).fetchall()]
        if not dates:
            return [], []
        placeholders = ",".join("?" * len(dates))
        # 金額用 price_history 收盤估算（富邦資料無價格）；有 BSR 精確金額時優先用之
        rows = c.execute(
            f"""SELECT k.stock_id, k.stock_name,
                       SUM(k.buy_shares) AS buy_shares,
                       SUM(k.sell_shares) AS sell_shares,
                       SUM(COALESCE(k.buy_amount,  k.buy_shares  * p.close)) AS buy_amount,
                       SUM(COALESCE(k.sell_amount, k.sell_shares * p.close)) AS sell_amount,
                       SUM(k.net_shares) AS net_shares,
                       SUM(k.turnover) AS turnover
                FROM kgi_cityhall_daily k
                LEFT JOIN price_history p
                  ON p.stock_id = k.stock_id AND p.trade_date = k.trade_date
                WHERE k.trade_date IN ({placeholders})
                GROUP BY k.stock_id, k.stock_name
                ORDER BY turnover DESC
                LIMIT 10""",
            dates,
        ).fetchall()
        return dates, [dict(r) for r in rows]


def query_all_aggregate(dates):
    """All stocks (not just top 10) aggregated over the given dates.
    Used for analysis: market-wide totals, concentration, day-trade detection."""
    if not dates:
        return []
    placeholders = ",".join("?" * len(dates))
    with get_conn() as c:
        rows = c.execute(
            f"""SELECT k.stock_id, k.stock_name,
                       SUM(k.buy_shares) AS buy_shares,
                       SUM(k.sell_shares) AS sell_shares,
                       SUM(COALESCE(k.buy_amount,  k.buy_shares  * p.close)) AS buy_amount,
                       SUM(COALESCE(k.sell_amount, k.sell_shares * p.close)) AS sell_amount,
                       SUM(k.net_shares) AS net_shares,
                       SUM(k.turnover) AS turnover
                FROM kgi_cityhall_daily k
                LEFT JOIN price_history p
                  ON p.stock_id = k.stock_id AND p.trade_date = k.trade_date
                WHERE k.trade_date IN ({placeholders})
                GROUP BY k.stock_id, k.stock_name""",
            list(dates),
        ).fetchall()
    return [dict(r) for r in rows]


def query_daily_series(stock_id, dates):
    """Return per-day net_shares for a stock across the given dates."""
    placeholders = ",".join("?" * len(dates))
    with get_conn() as c:
        rows = c.execute(
            f"""SELECT trade_date, net_shares FROM kgi_cityhall_daily
                WHERE stock_id=? AND trade_date IN ({placeholders})
                ORDER BY trade_date ASC""",
            [stock_id] + list(dates),
        ).fetchall()
    by_date = {r["trade_date"]: r["net_shares"] for r in rows}
    return [by_date.get(d, 0) for d in sorted(dates)]


def query_errors(trade_date):
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT stock_id, error_msg FROM fetch_errors WHERE trade_date=?",
            (trade_date,)).fetchall()]


def query_coverage():
    """Stats: distinct trade days, total kgi rows, errors today."""
    with get_conn() as c:
        days = c.execute("SELECT COUNT(DISTINCT trade_date) FROM kgi_cityhall_daily").fetchone()[0]
        rows = c.execute("SELECT COUNT(*) FROM kgi_cityhall_daily").fetchone()[0]
        return {"days": days, "rows": rows}


# ============================================================
# Price history helpers (for event study / backtest)
# ============================================================

def upsert_prices(stock_id, rows):
    """rows: list of dict {trade_date, open, high, low, close, volume}"""
    if not rows:
        return
    with get_conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO price_history
               (stock_id, trade_date, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(stock_id, r["trade_date"], r.get("open"), r.get("high"),
              r.get("low"), r.get("close"), r.get("volume")) for r in rows],
        )


def query_price_range(stock_id):
    """Return (min_date, max_date, count) of stored prices for a stock."""
    with get_conn() as c:
        row = c.execute(
            """SELECT MIN(trade_date), MAX(trade_date), COUNT(*)
               FROM price_history WHERE stock_id=?""", (stock_id,)).fetchone()
    return row[0], row[1], row[2]


def query_prices(stock_id, start_date=None, end_date=None):
    """Return ordered list of price dicts for a stock within optional range."""
    q = "SELECT trade_date, open, high, low, close, volume FROM price_history WHERE stock_id=?"
    args = [stock_id]
    if start_date:
        q += " AND trade_date >= ?"; args.append(start_date)
    if end_date:
        q += " AND trade_date <= ?"; args.append(end_date)
    q += " ORDER BY trade_date ASC"
    with get_conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def signal_stock_ids():
    """All distinct stock_ids ever recorded in kgi_cityhall_daily (= signal universe)."""
    with get_conn() as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT stock_id FROM kgi_cityhall_daily").fetchall()]


def earliest_signal_date():
    with get_conn() as c:
        row = c.execute("SELECT MIN(trade_date) FROM kgi_cityhall_daily").fetchone()
    return row[0] if row else None


def price_coverage():
    with get_conn() as c:
        stocks = c.execute("SELECT COUNT(DISTINCT stock_id) FROM price_history").fetchone()[0]
        rows = c.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        return {"stocks": stocks, "rows": rows}


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {DB_PATH}")
    print("kgi:", query_coverage())
    print("price:", price_coverage())
