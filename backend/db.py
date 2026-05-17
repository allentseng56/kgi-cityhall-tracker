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

CREATE INDEX IF NOT EXISTS idx_kgi_date ON kgi_cityhall_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_kgi_stock ON kgi_cityhall_daily(stock_id);
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
        rows = c.execute(
            f"""SELECT stock_id, stock_name,
                       SUM(buy_shares) AS buy_shares,
                       SUM(sell_shares) AS sell_shares,
                       SUM(buy_amount) AS buy_amount,
                       SUM(sell_amount) AS sell_amount,
                       SUM(net_shares) AS net_shares,
                       SUM(turnover) AS turnover
                FROM kgi_cityhall_daily
                WHERE trade_date IN ({placeholders})
                GROUP BY stock_id, stock_name
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
            f"""SELECT stock_id, stock_name,
                       SUM(buy_shares) AS buy_shares,
                       SUM(sell_shares) AS sell_shares,
                       SUM(buy_amount) AS buy_amount,
                       SUM(sell_amount) AS sell_amount,
                       SUM(net_shares) AS net_shares,
                       SUM(turnover) AS turnover
                FROM kgi_cityhall_daily
                WHERE trade_date IN ({placeholders})
                GROUP BY stock_id, stock_name""",
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


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {DB_PATH}")
    print(query_coverage())
