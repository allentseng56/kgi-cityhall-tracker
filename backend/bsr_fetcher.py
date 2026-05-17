# -*- coding: utf-8 -*-
"""
TWSE BSR (個股當日分點進出明細) fetcher with CAPTCHA OCR.

Flow:
  1. GET bsMenu.aspx → parse __VIEWSTATE / __EVENTVALIDATION + CAPTCHA img URL
  2. Download CAPTCHA → OCR via ddddocr
  3. POST stock_id + captcha + viewstate → success returns result page (with download link)
  4. GET CSV/HTML result → parse broker rows → filter & aggregate target broker
"""
import io
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE = "https://bsr.twse.com.tw/bshtm/"
MENU_URL = BASE + "bsMenu.aspx"
CONTENT_URL = BASE + "bsContent.aspx"

# Lazy global OCR instances (first call is slow due to model load).
# BSR captcha is exactly 5 chars from [A-Z0-9].
_CAPTCHA_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_OCR_MAIN = None
_OCR_BETA = None

def _ocr_main():
    global _OCR_MAIN
    if _OCR_MAIN is None:
        import ddddocr
        _OCR_MAIN = ddddocr.DdddOcr(show_ad=False)
        _OCR_MAIN.set_ranges(_CAPTCHA_CHARSET)
    return _OCR_MAIN

def _ocr_beta():
    global _OCR_BETA
    if _OCR_BETA is None:
        import ddddocr
        _OCR_BETA = ddddocr.DdddOcr(beta=True, show_ad=False)
        _OCR_BETA.set_ranges(_CAPTCHA_CHARSET)
    return _OCR_BETA


def _image_variants(img_bytes):
    """Generate several pre-processed versions for OCR voting."""
    from PIL import Image
    im = Image.open(io.BytesIO(img_bytes)).convert("L")
    variants = {"raw": img_bytes}
    for th in (140, 160, 180):
        bw = im.point(lambda p, t=th: 255 if p > t else 0, mode="L")
        buf = io.BytesIO(); bw.save(buf, format="PNG")
        variants[f"th{th}"] = buf.getvalue()
    for th in (140, 160):
        bw = im.point(lambda p, t=th: 0 if p > t else 255, mode="L")
        buf = io.BytesIO(); bw.save(buf, format="PNG")
        variants[f"inv{th}"] = buf.getvalue()
    return variants


class BsrError(Exception):
    pass


def _new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    })
    return s


def _parse_form(html):
    """Return dict of all hidden inputs and the CAPTCHA image URL."""
    soup = BeautifulSoup(html, "lxml")
    form_data = {}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        if name and inp.get("type") in (None, "hidden", "text", "submit"):
            form_data[name] = inp.get("value", "")
    # CAPTCHA image
    img = soup.find("img", id=re.compile("Captcha", re.I))
    if img is None:
        # fallback: any img referencing aspx
        for i in soup.find_all("img"):
            src = i.get("src", "")
            if "aspx" in src.lower() and "captcha" in src.lower():
                img = i
                break
    captcha_url = urljoin(MENU_URL, img["src"]) if img else None
    return form_data, captcha_url


def _ocr_captcha(session, captcha_url):
    """
    Run both ddddocr models against several image variants and pick the
    most-agreed-upon 5-char result. Returns ('', bytes) if nothing usable.
    """
    from collections import Counter
    r = session.get(captcha_url, timeout=15)
    r.raise_for_status()
    img_bytes = r.content
    variants = _image_variants(img_bytes)

    five_results = []
    all_results = []
    for ocr_fn in (_ocr_main, _ocr_beta):
        for b in variants.values():
            try:
                t = ocr_fn().classification(b)
                t = re.sub(r"[^A-Z0-9]", "", t.upper())
            except Exception:
                continue
            all_results.append(t)
            if len(t) == 5:
                five_results.append(t)
    if five_results:
        return Counter(five_results).most_common(1)[0][0], img_bytes
    return "", img_bytes


def _submit(session, form_data, stock_id, captcha_text):
    # Fill in the stock ID and captcha fields. Field names vary slightly
    # across page versions; cover the common ones.
    for key in list(form_data.keys()):
        kl = key.lower()
        if "stkno" in kl or "txtcode" in kl:
            form_data[key] = stock_id
        elif "captcha" in kl and "control" not in kl:
            form_data[key] = captcha_text
        elif key == "CaptchaControl1":
            form_data[key] = captcha_text
    # Ensure submit button is present (some pages need btnOK)
    if "btnOK" not in form_data:
        form_data["btnOK"] = "查詢"
    # The page has a "一般交易 / 鉅額交易" radio group; pick 一般交易
    form_data["RadioButton_Normal"] = "RadioButton_Normal"
    # Drop the reset button so it's not interpreted as the action
    form_data.pop("Button_Reset", None)
    r = session.post(MENU_URL, data=form_data, timeout=20, allow_redirects=True)
    r.raise_for_status()
    return r


def _extract_download_link(html):
    """After successful submit, page contains a link to bsContent.aspx?StkNo=...."""
    m = re.search(r'href=["\']([^"\']*bsContent\.aspx\?[^"\']+)["\']', html, re.I)
    if m:
        return urljoin(MENU_URL, m.group(1))
    return None


def _parse_content_html(html):
    """
    Parse bsContent.aspx HTML. Each data table has 5 columns:
      序 | 證券商 | 成交單價 | 買進股數 | 賣出股數
    Where 證券商 cell shows the full name only on the broker's first row
    (e.g. '920D 凱基市府'); subsequent rows for the same broker show just
    the code ('920D'). We extract the broker code (first whitespace-split
    token) as the canonical key, and carry the last full name forward.

    Returns list of dicts:
      {broker_code, broker_full, price, buy_shares, sell_shares}
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    # Map broker_code -> latest full name seen (for label resolution)
    code_to_name = {}
    seen_tables = set()
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        # First row must look like the 5-column header
        header_cells = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if len(header_cells) != 5:
            continue
        if not ("買進股數" in "".join(header_cells) and "賣出股數" in "".join(header_cells)):
            continue
        # Dedupe identical tables (the page nests duplicates for layout)
        sig = id(tbl)
        if sig in seen_tables:
            continue
        seen_tables.add(sig)
        for tr in rows[1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) != 5:
                continue
            broker_cell = cells[1]
            # Broker code = first whitespace-delimited token
            parts = broker_cell.split(None, 1)
            if not parts:
                continue
            code = parts[0]
            if len(parts) == 2:
                code_to_name[code] = broker_cell  # full label e.g. '920D 凱基市府'
            full = code_to_name.get(code, broker_cell)
            try:
                price = float(cells[2].replace(",", ""))
                buy = int(cells[3].replace(",", ""))
                sell = int(cells[4].replace(",", ""))
            except ValueError:
                continue
            if buy == 0 and sell == 0:
                continue
            out.append({
                "broker_code": code,
                "broker_full": full,
                "price": price,
                "buy_shares": buy,
                "sell_shares": sell,
            })
    return out


def aggregate_broker(records, broker_match="凱基市府"):
    """
    Sum buy/sell shares & weighted avg price for the matching broker.
    Matches against broker_full label (e.g. '920D 凱基市府') via substring.
    """
    buy_shares = sell_shares = 0
    buy_val = sell_val = 0.0
    matched = set()
    for r in records:
        if broker_match in r["broker_full"]:
            matched.add(r["broker_full"])
            if r["buy_shares"]:
                buy_shares += r["buy_shares"]
                buy_val += r["buy_shares"] * r["price"]
            if r["sell_shares"]:
                sell_shares += r["sell_shares"]
                sell_val += r["sell_shares"] * r["price"]
    if buy_shares == 0 and sell_shares == 0:
        return None
    return {
        "buy_shares": buy_shares,
        "sell_shares": sell_shares,
        "avg_buy_price": (buy_val / buy_shares) if buy_shares else None,
        "avg_sell_price": (sell_val / sell_shares) if sell_shares else None,
        "matched_brokers": sorted(matched),
    }


def fetch_stock_bsr(stock_id, max_attempts=15, broker_match="凱基市府",
                    pause_between=1.5, verbose=False):
    """
    High-level: fetch + parse BSR for one stock, retrying on CAPTCHA failure.
    Returns (aggregate_dict_or_None, raw_records, attempts_used).
    Raises BsrError on hard failure.
    """
    session = _new_session()
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = session.get(MENU_URL, timeout=20)
            r.raise_for_status()
            form_data, captcha_url = _parse_form(r.text)
            if not captcha_url:
                raise BsrError("CAPTCHA image not found on bsMenu page")
            captcha_text, _ = _ocr_captcha(session, captcha_url)
            if len(captcha_text) != 5:
                if verbose:
                    print(f"  attempt {attempt}: no 5-char OCR consensus, retry")
                last_err = "no 5-char ocr consensus"
                time.sleep(0.5)
                continue
            if verbose:
                print(f"  attempt {attempt}: captcha guess = {captcha_text}")
            resp = _submit(session, form_data, stock_id, captcha_text)
            # Check for error indication (e.g. 驗證碼錯誤)
            if "驗證碼" in resp.text and ("錯誤" in resp.text or "不正確" in resp.text):
                last_err = "captcha rejected"
                time.sleep(1.0)
                continue
            link = _extract_download_link(resp.text)
            if link:
                cr = session.get(link, timeout=20)
                cr.raise_for_status()
                html = cr.text
            else:
                # Maybe the result was rendered inline
                html = resp.text
            records = _parse_content_html(html)
            if not records:
                # Dump for debugging
                from pathlib import Path
                dbg = Path(__file__).resolve().parent.parent / "data" / "debug"
                dbg.mkdir(exist_ok=True)
                (dbg / f"empty_attempt{attempt}_submit.html").write_text(resp.text, encoding="utf-8")
                if link:
                    (dbg / f"empty_attempt{attempt}_content.html").write_text(html, encoding="utf-8")
                if verbose:
                    print(f"    submit URL={resp.url}, link={link}, "
                          f"html_len={len(html)}, has_驗證={('驗證' in resp.text)}")
                last_err = "no broker rows parsed"
                time.sleep(1.0)
                continue
            agg = aggregate_broker(records, broker_match=broker_match)
            return agg, records, attempt
        except requests.RequestException as e:
            last_err = f"network: {e}"
            time.sleep(2.0)
        time.sleep(pause_between)
    raise BsrError(f"Failed after {max_attempts} attempts: {last_err}")


if __name__ == "__main__":
    import sys
    sid = sys.argv[1] if len(sys.argv) > 1 else "2330"
    print(f"Fetching BSR for {sid} ...")
    agg, records, n = fetch_stock_bsr(sid, verbose=True)
    print(f"Done in {n} attempt(s). Parsed {len(records)} broker rows.")
    if agg:
        print(f"Matched brokers: {agg['matched_brokers']}")
        bp = f"{agg['avg_buy_price']:.2f}" if agg['avg_buy_price'] else "--"
        sp = f"{agg['avg_sell_price']:.2f}" if agg['avg_sell_price'] else "--"
        print(f"  Buy:  {agg['buy_shares']:>10,} shares @ {bp}")
        print(f"  Sell: {agg['sell_shares']:>10,} shares @ {sp}")
    else:
        print("No matching broker rows for this stock.")
