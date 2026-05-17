# -*- coding: utf-8 -*-
"""
KGI City-Hall Tracker — daily orchestrator.

Pipeline:
  1. Fetch market top-N stocks by volume from TWSE
  2. For each: fetch BSR, filter 凱基市府, write to SQLite
  3. Query last 7 trading days, build top-10 turnover ranking
  4. Render dashboard.html
"""
import argparse
import json
import sys
import time
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
from twse_top100 import fetch_top_n
from bsr_fetcher import fetch_stock_bsr, BsrError

ROOT = Path(__file__).resolve().parent.parent
OUT_HTML = ROOT / "output" / "dashboard.html"
TEMPLATE = Path(__file__).resolve().parent / "template.html"

BROKER_MATCH = "凱基市府"
LOOKBACK_DAYS = 7


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def render_dashboard(payload):
    template = TEMPLATE.read_text(encoding="utf-8")
    # JSON is injected into a <script type="application/json"> block. We must
    # only escape `</` so a literal "</script" inside data can't close the tag.
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    out = template.replace("/*__DATA__*/", blob)
    OUT_HTML.parent.mkdir(exist_ok=True)
    OUT_HTML.write_text(out, encoding="utf-8")


def _safe_div(a, b):
    return (a / b) if b else 0


def build_analysis(top10_all, lookback_days):
    """
    Generate human-readable interpretation of 凱基市府 activity from the
    full kgi_cityhall_daily aggregate rows (NOT just top 10 — we need
    market-wide totals).

    top10_all: list of all-stock aggregate dicts (stock_id, stock_name,
               buy_shares, sell_shares, buy_amount, sell_amount,
               net_shares, turnover)
    """
    if not top10_all:
        return None

    total_buy_shares = sum(r["buy_shares"] for r in top10_all)
    total_sell_shares = sum(r["sell_shares"] for r in top10_all)
    total_buy_amount = sum(r["buy_amount"] or 0 for r in top10_all)
    total_sell_amount = sum(r["sell_amount"] or 0 for r in top10_all)
    total_turnover = total_buy_shares + total_sell_shares
    total_turnover_amount = total_buy_amount + total_sell_amount
    net_shares = total_buy_shares - total_sell_shares
    net_amount = total_buy_amount - total_sell_amount

    # Sort copies by different criteria
    by_net_buy = sorted(top10_all, key=lambda r: r["net_shares"], reverse=True)
    by_net_sell = sorted(top10_all, key=lambda r: r["net_shares"])
    by_amount = sorted(top10_all,
                       key=lambda r: (r["buy_amount"] or 0) + (r["sell_amount"] or 0),
                       reverse=True)

    # ---------- Overall verdict ----------
    bias_amount_ratio = _safe_div(net_amount, total_turnover_amount) if total_turnover_amount else 0
    abs_bias = abs(bias_amount_ratio)
    n_stocks = len(top10_all)
    n_net_buy = sum(1 for r in top10_all if r["net_shares"] > 0)
    n_net_sell = sum(1 for r in top10_all if r["net_shares"] < 0)

    if abs_bias < 0.05:
        verdict_class = "flat"
        bias_word = "雙向操作、無顯著方向性"
    elif bias_amount_ratio > 0:
        verdict_class = "buy"
        bias_word = f"偏買（淨買佔總進出金額 {bias_amount_ratio*100:.1f}%）"
    else:
        verdict_class = "sell"
        bias_word = f"偏賣（淨賣佔總進出金額 {abs_bias*100:.1f}%）"

    verdict_text = (
        f"涵蓋 {n_stocks} 檔股票，總進出金額約 {total_turnover_amount/1e8:.2f} 億元，"
        f"{bias_word}。淨買 {n_net_buy} 檔、淨賣 {n_net_sell} 檔。"
    )

    # ---------- Top buy interpretations (淨買金額) ----------
    buy_interps = []
    for r in by_net_buy[:3]:
        if r["net_shares"] <= 0:
            break
        bs, ss = r["buy_shares"], r["sell_shares"]
        bp = _safe_div(r["buy_amount"] or 0, bs) if bs else 0
        sp = _safe_div(r["sell_amount"] or 0, ss) if ss else 0
        share_of_buy = _safe_div(r["buy_amount"] or 0, total_buy_amount)
        # Behavioral interpretation
        if ss == 0:
            behavior = "<strong>單向買進、零賣出</strong>，建倉意圖明確"
        elif bs >= ss * 5:
            behavior = f"買量為賣量 {bs/ss:.1f} 倍，<strong>強力買超</strong>"
        else:
            behavior = "買賣同步、淨買幅度有限"
        # Price comparison
        if bp and sp:
            if bp < sp:
                price_note = f"買進均價 {bp:.2f} 低於賣出均價 {sp:.2f}（價差 {sp-bp:.2f}），<strong>低買高賣</strong>順向交易"
            else:
                price_note = f"買進均價 {bp:.2f} 高於賣出均價 {sp:.2f}，買在相對高點"
        elif bp:
            price_note = f"買進均價 {bp:.2f}"
        else:
            price_note = ""
        msg = (f"<strong>{r['stock_id']} {r['stock_name']}</strong> "
               f"<span class='tag tag-buy'>淨買 {r['net_shares']:,} 股</span> "
               f"買進金額 {(r['buy_amount'] or 0)/1e4:,.0f} 萬"
               f"（佔當日總買進 {share_of_buy*100:.1f}%）。{behavior}；{price_note}")
        buy_interps.append(msg)

    # ---------- Top sell interpretations ----------
    sell_interps = []
    for r in by_net_sell[:3]:
        if r["net_shares"] >= 0:
            break
        bs, ss = r["buy_shares"], r["sell_shares"]
        bp = _safe_div(r["buy_amount"] or 0, bs) if bs else 0
        sp = _safe_div(r["sell_amount"] or 0, ss) if ss else 0
        share_of_sell = _safe_div(r["sell_amount"] or 0, total_sell_amount)
        if bs == 0:
            behavior = "<strong>單向賣出、零買進</strong>，出場意圖明確"
        elif ss >= bs * 5:
            behavior = f"賣量為買量 {ss/bs:.1f} 倍，<strong>強力賣超</strong>"
        else:
            behavior = "買賣同步、淨賣幅度有限"
        if bp and sp:
            if sp > bp:
                price_note = f"賣出均價 {sp:.2f} 高於買進均價 {bp:.2f}（價差 {sp-bp:.2f}），<strong>高賣低買</strong>順向交易"
            else:
                price_note = f"賣出均價 {sp:.2f} 低於買進均價 {bp:.2f}，賣在相對低點"
        elif sp:
            price_note = f"賣出均價 {sp:.2f}"
        else:
            price_note = ""
        msg = (f"<strong>{r['stock_id']} {r['stock_name']}</strong> "
               f"<span class='tag tag-sell'>淨賣 {abs(r['net_shares']):,} 股</span> "
               f"賣出金額 {(r['sell_amount'] or 0)/1e4:,.0f} 萬"
               f"（佔當日總賣出 {share_of_sell*100:.1f}%）。{behavior}；{price_note}")
        sell_interps.append(msg)

    # ---------- Structural observations ----------
    struct = []
    # Concentration (by amount)
    if by_amount and total_turnover_amount:
        top1_share = _safe_div((by_amount[0]["buy_amount"] or 0) + (by_amount[0]["sell_amount"] or 0),
                                total_turnover_amount)
        top3_share = _safe_div(
            sum((r["buy_amount"] or 0) + (r["sell_amount"] or 0) for r in by_amount[:3]),
            total_turnover_amount)
        top1 = by_amount[0]
        struct.append(
            f"<strong>集中度</strong>：金額最大標的 "
            f"<strong>{top1['stock_id']} {top1['stock_name']}</strong> 佔總進出金額 "
            f"{top1_share*100:.1f}%；前 3 大標的佔 {top3_share*100:.1f}%。"
            + ("<span class='tag tag-sell'>高度集中</span>" if top1_share > 0.3
               else "<span class='tag tag-buy'>分散</span>" if top1_share < 0.1 else "")
        )

    # Day-trader pattern detection (high turnover with near-zero net)
    day_trade_candidates = [r for r in top10_all
                            if r["turnover"] > 50000
                            and abs(r["net_shares"]) < r["turnover"] * 0.1
                            and r["buy_shares"] > 0 and r["sell_shares"] > 0]
    if day_trade_candidates:
        names = "、".join(f"{r['stock_id']} {r['stock_name']}" for r in day_trade_candidates[:3])
        struct.append(
            f"<strong>疑似當沖</strong>：{names} 等 {len(day_trade_candidates)} 檔買賣量大但淨額接近零，"
            "研判為日內進出操作（非建倉/出場）。"
        )

    # Pure-buy (zero-sell) signals — high conviction
    pure_buys = [r for r in top10_all if r["sell_shares"] == 0 and r["buy_shares"] > 1000]
    if pure_buys:
        pure_buys.sort(key=lambda r: r["buy_amount"] or 0, reverse=True)
        names = "、".join(f"{r['stock_id']} {r['stock_name']}({r['buy_shares']:,})"
                          for r in pure_buys[:5])
        struct.append(
            f"<strong>純買零賣</strong>（高度建倉訊號）：{names}。"
            f"共 {len(pure_buys)} 檔。"
        )

    # Pure-sell (zero-buy) signals
    pure_sells = [r for r in top10_all if r["buy_shares"] == 0 and r["sell_shares"] > 1000]
    if pure_sells:
        pure_sells.sort(key=lambda r: r["sell_amount"] or 0, reverse=True)
        names = "、".join(f"{r['stock_id']} {r['stock_name']}({r['sell_shares']:,})"
                          for r in pure_sells[:5])
        struct.append(
            f"<strong>純賣零買</strong>（高度出場訊號）：{names}。"
            f"共 {len(pure_sells)} 檔。"
        )

    return {
        "total_buy_shares": total_buy_shares,
        "total_sell_shares": total_sell_shares,
        "total_buy_amount": total_buy_amount,
        "total_sell_amount": total_sell_amount,
        "total_turnover_shares": total_turnover,
        "total_turnover_amount": total_turnover_amount,
        "net_shares": net_shares,
        "net_amount": net_amount,
        "verdict_class": verdict_class,
        "verdict_text": verdict_text,
        "buy_interpretations": buy_interps,
        "sell_interpretations": sell_interps,
        "structural_observations": struct,
    }


def run(top_n=100, max_per_stock=15, pause=2.0, open_browser=True, skip_fetch=False):
    db.init_db()
    trade_date = today_str()
    print(f"[{datetime.now():%H:%M:%S}] Run start. trade_date={trade_date}, top_n={top_n}")

    if not skip_fetch:
        # Step 1: TWSE top-N (returns the actual trade date used)
        try:
            trade_date, top = fetch_top_n(top_n)
            print(f"  [TWSE] trade_date={trade_date}, top {len(top)} fetched")
            db.upsert_top100(trade_date, top)
        except Exception as e:
            print(f"  [TWSE] FAILED: {e}")
            traceback.print_exc()
            top = []

        # Skip BSR fetch if this trade_date is already well-covered in DB
        # (e.g. weekend/holiday cron firings would otherwise re-fetch Friday's
        # data and waste ~10 min of GitHub Actions minutes)
        with db.get_conn() as conn:
            existing = conn.execute(
                "SELECT COUNT(*) FROM kgi_cityhall_daily WHERE trade_date=?",
                (trade_date,)).fetchone()[0]
        if existing >= 50:
            print(f"  [SKIP] trade_date={trade_date} already has {existing} rows; "
                  f"likely a holiday/weekend re-run. Skipping BSR fetch.")
            top = []

        # Step 2: BSR per-stock
        success = 0
        skipped = 0
        errors = 0
        for r in top:
            sid = r["stock_id"]
            name = r["stock_name"]
            print(f"  [{r['rank']:3d}/{len(top)}] {sid} {name} ... ", end="", flush=True)
            try:
                agg, _records, attempts = fetch_stock_bsr(
                    sid, max_attempts=max_per_stock, broker_match=BROKER_MATCH,
                    pause_between=1.0, verbose=False)
                if agg is None:
                    print(f"no 凱基市府 activity (attempts={attempts})")
                    skipped += 1
                else:
                    db.upsert_kgi_row(trade_date, sid, name, agg)
                    db.clear_error(trade_date, sid)
                    print(f"buy={agg['buy_shares']:,} sell={agg['sell_shares']:,} "
                          f"(attempts={attempts})")
                    success += 1
            except BsrError as e:
                print(f"FAIL: {e}")
                db.log_error(trade_date, sid, str(e))
                errors += 1
            except Exception as e:
                print(f"EXCEPTION: {e}")
                db.log_error(trade_date, sid, f"unexpected: {e}")
                errors += 1
            time.sleep(pause)

        print(f"\n  Summary: success={success} no-activity={skipped} errors={errors}")

    # Step 3: query & render
    dates, top10 = db.query_top10_recent(days=LOOKBACK_DAYS)
    coverage = db.query_coverage()
    errs = db.query_errors(trade_date)

    # Per-stock daily series for sparklines
    for row in top10:
        row["series"] = db.query_daily_series(row["stock_id"], dates)

    # Analysis uses ALL stocks (not just top 10) for accurate totals & concentration
    all_agg = db.query_all_aggregate(dates)
    analysis = build_analysis(all_agg, LOOKBACK_DAYS)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "broker": BROKER_MATCH,
        "lookback_days": LOOKBACK_DAYS,
        "covered_dates": sorted(dates),
        "coverage": coverage,
        "top10": top10,
        "analysis": analysis,
        "errors": errs,
    }
    render_dashboard(payload)
    print(f"\n  Dashboard written: {OUT_HTML}")

    if open_browser:
        webbrowser.open(OUT_HTML.as_uri())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=100, help="how many top-volume stocks to scan")
    ap.add_argument("--max-per-stock", type=int, default=15,
                    help="max BSR retry attempts per stock")
    ap.add_argument("--pause", type=float, default=2.0,
                    help="seconds between stocks")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--render-only", action="store_true",
                    help="skip fetch, only re-render dashboard from existing DB")
    args = ap.parse_args()
    run(top_n=args.top, max_per_stock=args.max_per_stock, pause=args.pause,
        open_browser=not args.no_browser, skip_fetch=args.render_only)
