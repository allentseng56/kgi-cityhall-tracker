# -*- coding: utf-8 -*-
"""
富邦 DJ (fubon-ebrokerdj) 分點歷史抓取。

提供 TWSE BSR 缺少的「歷史」分點資料（BSR 只有當日）。
資料為「張」(= 1000 股)，四捨五入，已與官方 BSR 交叉驗證吻合。

分點代碼編碼：富邦 DJ 的 `b` 參數是把分點代碼（如 "920D"）每個字元
轉成 4 位十六進位 ASCII。例： '9'->0039, '2'->0032, '0'->0030, 'D'->0044
=> "920D" => "0039003200300044"。

來源頁面：
  zco0.djhtm?a=<股票>&b=<編碼分點>&BHID=<券商>&C=1&D=<起>&E=<迄>&ver=V3
"""
import re
import time
import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ZCO0 = "https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco0/zco0.djhtm"

# 凱基(券商)=9200；市府(分點)=920D
KGI_BROKER_BHID = "9200"
KGI_CITYHALL_BRANCH = "920D"


def encode_branch(branch_code):
    """ '920D' -> '0039003200300044' (each char -> 4-hex-digit ASCII)."""
    return "".join(format(ord(c), "04x") for c in branch_code)


def fetch_branch_series(stock_id, start, end,
                        branch_code=KGI_CITYHALL_BRANCH,
                        broker_bhid=KGI_BROKER_BHID,
                        b_raw=None,
                        timeout=30):
    """
    Return list of {trade_date, buy_lots, sell_lots} (張) for one branch on
    one stock over [start, end] (YYYY-MM-DD). Empty list if no activity.
    Raises requests.RequestException on network failure.

    b_raw: 若提供，直接當作 `b` 參數（用排行頁抓到的原始 b-code，純數字分點
    如凱基台北=9268 不可用 encode_branch）。BHID 在 b 為完整分點碼時可留空。
    """
    params = {
        "a": stock_id,
        "b": b_raw if b_raw is not None else encode_branch(branch_code),
        "BHID": broker_bhid,
        "C": "1",
        "D": start,
        "E": end,
        "ver": "V3",
    }
    r = requests.get(ZCO0, params=params, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    text = r.content.decode("big5", errors="replace")
    soup = BeautifulSoup(text, "lxml")

    out = []
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if not rows:
            continue
        hdr = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
        if len(hdr) == 5 and hdr[0] == "日期" and "買進" in hdr[1]:
            for tr in rows[1:]:
                cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) == 5 and re.match(r"\d{4}/\d{2}/\d{2}", cells[0]):
                    try:
                        out.append({
                            "trade_date": cells[0].replace("/", "-"),
                            "buy_lots": int(cells[1].replace(",", "")),
                            "sell_lots": int(cells[2].replace(",", "")),
                        })
                    except ValueError:
                        continue
            break
    return out


if __name__ == "__main__":
    import sys
    sid = sys.argv[1] if len(sys.argv) > 1 else "2330"
    start = sys.argv[2] if len(sys.argv) > 2 else "2026-05-01"
    end = sys.argv[3] if len(sys.argv) > 3 else "2026-05-16"
    rows = fetch_branch_series(sid, start, end)
    print(f"凱基市府 對 {sid} ({start}~{end}): {len(rows)} 天")
    for r in rows:
        print(f"  {r['trade_date']}  買 {r['buy_lots']:>5} 張  賣 {r['sell_lots']:>5} 張")
