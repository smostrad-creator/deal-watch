# -*- coding: utf-8 -*-
"""
測試：GitHub 的電腦能不能查 Google Flights？
連續查 3 次「台北 → 東京」，把結果傳到 Telegram，直接告訴你「可行」或「被擋」。
"""
import json
import os
import time
import urllib.request
from datetime import date, timedelta

ROUTES = [("TPE", "NRT", 30), ("TPE", "KIX", 45), ("TPE", "ICN", 60)]  # (出發, 目的地, 幾天後出發)


def send(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    print(text)
    if not token or not chat:
        return
    body = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20)


def check(frm, to, days):
    from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html, parser
    from selectolax.lexbor import LexborHTMLParser

    d1 = date.today() + timedelta(days=days)
    d2 = d1 + timedelta(days=4)
    q = create_query(
        flights=[FlightQuery(date=d1.isoformat(), from_airport=frm, to_airport=to),
                 FlightQuery(date=d2.isoformat(), from_airport=to, to_airport=frm)],
        trip="round-trip", passengers=Passengers(adults=1), language="zh-TW", currency="TWD", max_stops=0)
    label = f"{frm}→{to} {d1:%m/%d}～{d2:%m/%d}"
    try:
        html = fetch_flights_html(q)
    except Exception as e:
        return False, f"{label}：連線失敗（{type(e).__name__}: {str(e)[:80]}）"

    low = html.lower()
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        if "unusual traffic" in low or "/sorry/" in low or "captcha" in low:
            why = "Google 判定為異常流量，要求驗證（被擋）"
        elif "consent" in low:
            why = "被導到 Google 同意條款頁面"
        else:
            why = f"頁面裡沒有航班資料（頁面長度 {len(html)} 字）"
        return False, f"{label}：{why}"
    try:
        data = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
        flights = parser.parse_payload(json.loads(data), include_top_results=True)
    except Exception as e:
        return False, f"{label}：讀取資料失敗（{type(e).__name__}）"
    priced = [f for f in flights if f.price]
    if not priced:
        return False, f"{label}：有頁面但沒有航班價格"
    airlines = sorted({a for f in priced for a in (f.airlines or [])})
    best = min(priced, key=lambda f: f.price)
    return True, (f"{label}：找到 {len(priced)} 班，{len(airlines)} 家航空\n"
                  f"　最便宜 NT${int(best.price):,}（{'、'.join(best.airlines or [])}）\n"
                  f"　航空：{'、'.join(airlines[:8])}")


def main():
    results = []
    for i, (frm, to, days) in enumerate(ROUTES):
        if i:
            time.sleep(8)
        ok, msg = check(frm, to, days)
        results.append((ok, msg))
    n = sum(ok for ok, _ in results)
    if n == len(results):
        head = f"✅ 可行！{n}/{len(results)} 次都查到資料，GitHub 沒有被 Google 擋。"
    elif n:
        head = f"⚠️ 部分可行：{n}/{len(results)} 次成功。可能會時好時壞。"
    else:
        head = f"❌ 不可行：{len(results)} 次都失敗，GitHub 的電腦被 Google 擋了。"
    body = "\n\n".join(("✓ " if ok else "✗ ") + msg for ok, msg in results)
    send(f"【Google Flights 測試】\n{head}\n\n{body}")


if __name__ == "__main__":
    main()
