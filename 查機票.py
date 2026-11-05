# -*- coding: utf-8 -*-
"""
查機票.py — 用 Google Flights 的「真正來回票價」比價，同一家航空所有時段排在一起，輸出成 Excel

資料來源：Google Flights（透過免費開源套件 faster-flights）
第一次使用請先在終端機執行（三行依序）：
    pip uninstall -y fast-flights
    pip install --force-reinstall faster-flights
    pip install openpyxl

用法：
    1. 修改下面「設定區」
    2. 終端機輸入：python 查機票.py
    3. 跑完會在同一個資料夾產生 Excel 檔

想先看看排版（不連網、用假資料）：
    python 查機票.py --demo
"""

# ════════════════════════════════════════════════════════════
#                         設定區（改這裡就好）
# ════════════════════════════════════════════════════════════

出發地 = "TPE"          # 機場代碼：TPE 桃園、TSA 松山、KHH 高雄、RMQ 台中
目的地 = "HKG"          # 例如 HKG 香港、NRT 成田、HND 羽田、KIX 關西、ICN 仁川、BKK 曼谷

# ── 日期：兩種方式擇一 ──
# 方式 A：自己列出每一組（去程日, 回程日）
日期組合 = [
    ("2027-01-09", "2027-01-12"),
    ("2027-01-16", "2027-01-19"),
]

# 方式 B：自動產生「每週固定星期幾出發，玩幾天」
#   把 自動產生 改成 True 就會忽略上面的 日期組合
自動產生 = False
自動_開始日 = "2027-01-01"
自動_結束日 = "2027-02-28"
自動_出發星期 = 6            # 1=週一 … 5=週五 6=週六 7=週日
自動_回程隔幾天 = 3          # 例如週六出發、隔 3 天 = 週二回

# ── 轉機 ──
#   0 = 只要直飛　1 = 最多轉機 1 次　None = 不限
轉機 = 0

# ── 查詢方式 ──
#   False（建議）：每組日期查「1 + 航空公司家數」次。
#                 去程每一班都有來回價；回程價是搭配該航空「最便宜那班去程」的來回總價。
#   True        ：每一班去程都去查一次回程，列出所有「去程 × 回程」組合的來回價。
#                 最完整，但查詢次數多很多（例如 10 班去程就要多查 10 次），比較慢也比較容易被擋。
完整組合 = False

# ── 其他 ──
大人人數 = 1
只看這些航空公司 = []     # 空的 = 全部；或填代碼，例如 ["CX", "BR", "JX"]
去程最早出發 = None       # 例如 8 代表 08:00 以後出發；None = 不限
去程最晚出發 = None       # 例如 14 代表 14:59 以前出發
回程最早出發 = None
回程最晚出發 = None

輸出檔名 = "機票比價.xlsx"
每次查詢間隔秒數 = 3      # 別設太小，查太快可能被 Google 暫時擋住

# ════════════════════════════════════════════════════════════
#                     以下是程式本體，不用改
# ════════════════════════════════════════════════════════════

import json
import os
import re
import sys
import time
import random
from datetime import date, datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    print("缺少 openpyxl，請先執行：pip install openpyxl")
    sys.exit(1)

週 = "一二三四五六日"


# ─────────────────────────── 日期 ───────────────────────────
def build_date_pairs():
    if not 自動產生:
        return [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in 日期組合]
    start, end = date.fromisoformat(自動_開始日), date.fromisoformat(自動_結束日)
    d = start
    while d.isoweekday() != 自動_出發星期:
        d += timedelta(days=1)
    pairs = []
    while d <= end:
        pairs.append((d, d + timedelta(days=自動_回程隔幾天)))
        d += timedelta(days=7)
    return pairs


def date_label(d1, d2):
    return f"{d1.month}/{d1.day}({週[d1.weekday()]})\n~\n{d2.month}/{d2.day}({週[d2.weekday()]})"


# ─────────────────────────── 共用小工具 ───────────────────────────
def in_hours(hour, earliest, latest):
    return (earliest is None or hour >= earliest) and (latest is None or hour <= latest)


def hm(t):
    """Google 會省略 0：08:00 只給 [8]，00:31 給 [None, 31]，這裡補齊成 (時, 分)"""
    t = list(t or []) + [None, None]
    return (t[0] or 0, t[1] or 0)


def fmt_time(t):
    h, m = hm(t)
    return f"{h:02d}:{m:02d}"


INF = float("inf")


def pv(x):
    """排序用的價格：沒價格當成無限大"""
    return x["price"] if x.get("price") is not None else INF


def describe(f, allow_noprice=False):
    """把一筆 Google 結果轉成 dict；不合條件回傳 None。allow_noprice=True 時，沒價格的也保留（price=None）"""
    try:
        return _describe(f, allow_noprice)
    except Exception:
        return None


# Google 沒給航空公司名稱時，用航空代碼對照中文名
AIRLINE_NAMES = {
    "ZE": "易斯達航空", "LJ": "真航空", "BX": "釜山航空", "7C": "濟州航空", "TW": "德威航空",
    "RF": "Aero K 航空", "YP": "Air Premia", "KE": "大韓航空", "OZ": "韓亞航空",
    "IT": "台灣虎航", "CI": "中華航空", "BR": "長榮航空", "JX": "星宇航空", "B7": "立榮航空", "AE": "華信航空",
    "CX": "國泰航空", "HX": "香港航空", "UO": "香港快運", "NX": "澳門航空",
    "MM": "樂桃航空", "GK": "捷星日本航空", "3K": "捷星亞洲航空", "JQ": "捷星航空", "TR": "酷航",
    "NH": "全日空", "JL": "日本航空", "AK": "亞洲航空", "D7": "全亞洲航空", "FD": "泰國亞洲航空",
    "Z2": "菲律賓亞洲航空", "VJ": "越捷航空", "VN": "越南航空", "5J": "宿霧太平洋航空", "PR": "菲律賓航空",
    "SL": "泰國獅子航空", "TG": "泰國國際航空", "SQ": "新加坡航空", "MH": "馬來西亞航空",
    "CA": "中國國際航空", "MU": "中國東方航空", "CZ": "中國南方航空", "MF": "廈門航空",
}


def lenient_entry(e, code_to_name):
    """寬鬆讀取一筆航班：只要時間、機場、價格、航空公司；其他欄位缺了也沒關係"""
    from fast_flights.model import Airport, CarbonEmission, Flights, SimpleDatetime, SingleFlight

    def g(x, *idx):
        for i in idx:
            try:
                x = x[i]
            except (IndexError, TypeError, KeyError):
                return None
        return x

    flight = g(e, 0)
    segs_raw = g(flight, 2)
    if not isinstance(segs_raw, list) or not segs_raw:
        return None
    segs = []
    for sf in segs_raw:
        dep_t, dep_d = g(sf, 8), g(sf, 20)
        arr_t, arr_d = g(sf, 10), g(sf, 21)
        if not dep_d or not arr_d:
            return None
        code = g(sf, 22, 0)
        segs.append(SingleFlight(
            from_airport=Airport(code=g(sf, 3) or "", name=g(sf, 4) or ""),
            to_airport=Airport(code=g(sf, 6) or "", name=g(sf, 5) or ""),
            departure=SimpleDatetime(date=dep_d, time=dep_t or []),
            arrival=SimpleDatetime(date=arr_d, time=arr_t or []),
            duration=g(sf, 11), plane_type=g(sf, 17),
            airline_code=code if isinstance(code, str) else "",
            flight_number=g(sf, 22, 1) if isinstance(g(sf, 22, 1), str) else "",
        ))
    names = g(flight, 1)
    if not isinstance(names, list) or not names:
        names = []
        for x in segs:
            if x.airline_code:
                n = code_to_name.get(x.airline_code) or AIRLINE_NAMES.get(x.airline_code, x.airline_code)
                if n not in names:
                    names.append(n)
    tok = g(e, 1, 1)
    return Flights(
        type=g(flight, 0), price=g(e, 1, 0, 1), airlines=names, flights=segs,
        carbon=CarbonEmission(typical_on_route=g(flight, 22, 8), emission=g(flight, 22, 7)),
        select_token=tok if isinstance(tok, str) else None,
        select_data=g(e, 8) if isinstance(g(e, 8), str) else None,
    )


FAIL_NOTE = ""  # 有日期查詢失敗時的說明（結果不完整）
NOTES = []  # 查詢過程中的提醒（例如 Google 沒給價格的航班），網頁版會顯示在結果下方


def _describe(f, allow_noprice=False):
    segs = f.flights
    if not segs:
        return None
    if not f.price and not allow_noprice:
        try:
            name = " / ".join(dict.fromkeys(f.airlines or [])) or "未知航空"
            route = f"{segs[0].from_airport.code} → {segs[-1].to_airport.code}"
            msg = f"{name}（{route}）：Google 清單上沒有價格，要點進 Google Flights 才看得到"
            if msg not in NOTES:
                NOTES.append(msg)
                print(f"      ℹ {msg}")
        except Exception:
            pass
        return None
    first, last = segs[0], segs[-1]
    text = f"{fmt_time(first.departure.time)} - {fmt_time(last.arrival.time)}"
    days = (date(*last.arrival.date) - date(*first.departure.date)).days
    if days > 0:
        text += f" +{days}"
    if len(segs) > 1:
        text += "（經 " + "、".join(s.to_airport.code for s in segs[:-1]) + "）"
    if getattr(f, "ow_sum", False):
        text += "（單程相加）"
    h, m = hm(first.departure.time)
    return {
        "airline": " / ".join(dict.fromkeys(f.airlines or [])) or "未知航空",
        "text": text,
        "price": int(f.price) if f.price else None,
        "key": h * 60 + m,
        "hour": h,
        "obj": f,
    }


def dedupe_sort(items):
    best = {}
    for it in items:
        if it["text"] not in best or pv(it) < pv(best[it["text"]]):
            best[it["text"]] = it
    return sorted(best.values(), key=lambda x: x["key"])


def by_airline(items):
    out = {}
    for it in items:
        out.setdefault(it["airline"], []).append(it)
    return {a: dedupe_sort(v) for a, v in out.items()}


# ─────────────────────────── 真的去查 ───────────────────────────
def make_query(d1, d2, language="zh-TW"):
    from fast_flights import FlightQuery, Passengers, create_query

    al = 只看這些航空公司 or None
    return create_query(
        flights=[
            FlightQuery(date=d1.isoformat(), from_airport=出發地, to_airport=目的地, airlines=al),
            FlightQuery(date=d2.isoformat(), from_airport=目的地, to_airport=出發地, airlines=al),
        ],
        trip="round-trip",
        passengers=Passengers(adults=大人人數),
        language=language,
        currency="TWD",
        max_stops=轉機,
    )


class 已停止(Exception):
    """使用者按了「停止」"""


def should_stop():
    """網頁版會把這個換掉，用來偵測使用者有沒有按「停止」"""
    return False


def nap(seconds):
    """可以被「停止」打斷的等待"""
    end = time.time() + seconds
    while time.time() < end:
        if should_stop():
            raise 已停止()
        time.sleep(0.2)


class 查詢失敗(Exception):
    """重試好幾次都失敗（可能被 Google 暫時擋住）"""


def polite_call(fn, *args, strict=False):
    """查詢 + 自動重試 + 間隔。strict=True 時，全部失敗會丟出「查詢失敗」，不會當成沒有航班"""
    waits = [10, 25, 45]
    last = None
    for attempt in range(len(waits) + 1):
        if should_stop():
            raise 已停止()
        try:
            res = fn(*args)
        except Exception as e:
            last = e
            if attempt < len(waits):
                print(f"      ⚠ 查詢失敗（{type(e).__name__}: {str(e)[:60]}），{waits[attempt]} 秒後重試…")
                nap(waits[attempt])
            continue
        nap(每次查詢間隔秒數 + random.random())
        return res
    if strict:
        print("      ✗ 重試好幾次都失敗")
        raise 查詢失敗(f"{type(last).__name__}: {str(last)[:80]}")
    print("      ✗ 放棄這一筆")
    return []


def fetch_returns(q, out_item):
    """選定一班去程，抓回程（只留同一家航空）。回傳 (回程清單, 價格是否可靠)"""
    from fast_flights import get_return_flights, select_flight

    try:
        rq = select_flight(q, out_item["obj"])
    except Exception:
        return [], False
    raw = polite_call(get_return_flights, rq)
    # 只看同一家航空的回程。別家的回程不能跟這班去程一起買，Google 本來就不會給價格，
    # 不能算成「查不到價格」，不然會誤報（例如選了虎航去程，長榮的回程就沒價格）。
    me = out_item["airline"]
    raw = [f for f in raw if (" / ".join(dict.fromkeys(f.airlines or [])) or "未知航空") == me]
    rets = [r for r in (describe(f) for f in raw) if r]
    rets = [r for r in rets if r["airline"] == out_item["airline"]
            and in_hours(r["hour"], 回程最早出發, 回程最晚出發)]
    rets = dedupe_sort(rets)
    # 正常情況：最便宜的回程組合價 ≈ 這班去程顯示的來回價。
    # 如果低很多，代表 Google 這次只給了單程價（套件的備援機制），要標示出來。
    ok = bool(rets) and min(r["price"] for r in rets) >= out_item["price"] * 0.85
    return rets, ok


_FULL_PARSER = False


def use_full_results():
    """套件預設只讀 Google 的「其他航班」，漏掉「最佳航班」那一群。這裡改成兩群都讀。"""
    global _FULL_PARSER
    if _FULL_PARSER:
        return
    import json
    from fast_flights import fetcher, parser
    from selectolax.lexbor import LexborHTMLParser

    def full_parse(html):
        script = LexborHTMLParser(html).css_first(r"script.ds\:1")
        if script is None:
            raise ValueError("Google 回傳的頁面裡沒有航班資料，可能被暫時擋住，請稍後再試。")
        js = script.text()
        i = js.index("data:") + 5
        while js[i].isspace():
            i += 1
        payload, _ = json.JSONDecoder().raw_decode(js, i)  # 只讀 data: 後面完整的那一段，不怕後面格式不同
        return robust_parse_payload(payload)

    def robust_parse_payload(payload, **_ignored):
        # 有些航班 Google 列出來了，但清單上沒有價格（要到訂票網站才看得到），
        # 套件遇到沒價格的會整筆丟掉。這裡先補一個暫時價格讓它讀得進來，之後再標成「沒有價格」。
        raw_names = set()
        for gi in (2, 3):
            try:
                grp = payload[gi][0] or []
            except (IndexError, TypeError):
                grp = []
            for e in grp:
                try:
                    raw_names.update(e[0][1] or [])
                except Exception:
                    pass
                try:
                    has_price = e[1][0][1] is not None
                except Exception:
                    has_price = False
                if not has_price and isinstance(e, list) and len(e) > 1:
                    try:
                        tok = e[1][1] if isinstance(e[1], list) and len(e[1]) > 1 else None
                    except Exception:
                        tok = None
                    e[1] = [[None, -1], tok]
        flights = parser.parse_payload(payload, include_top_results=True)
        # 套件讀不了的航班（欄位缺少或格式不同，例如沒有 logo 的易斯達）改用寬鬆的方式讀
        try:
            code_to_name = {v: k for k, v in parser._parse_metadata(payload)[1].items() if v}
        except Exception:
            code_to_name = {}
        have = {(tuple(f.airlines or []), tuple((tuple(x.departure.time or []), x.from_airport.code) for x in f.flights))
                for f in flights}
        for gi in (2, 3):
            try:
                grp = payload[gi][0] or []
            except (IndexError, TypeError):
                grp = []
            for e in grp:
                try:
                    if parser._parse_flight_entry(e, {}) is not None:
                        continue
                except Exception:
                    pass
                f = lenient_entry(e, code_to_name)
                if f is None:
                    continue
                key = (tuple(f.airlines or []), tuple((tuple(x.departure.time or []), x.from_airport.code) for x in f.flights))
                if key not in have:
                    have.add(key)
                    flights.append(f)
        for f in flights:
            if f.price == -1:
                f.price = None
        parsed = {a for f in flights for a in (f.airlines or [])}
        lost = raw_names - parsed
        if lost:
            msg = "、".join(sorted(lost)) + "：Google 有列出，但資料格式讀不懂，表格沒辦法顯示"
            NOTES.append(msg)
            print(f"      ℹ {msg}")
        return flights

    parser.parse = full_parse
    fetcher.parse = full_parse
    try:
        from fast_flights import shopping as _shopping
        _shopping.parse_payload = robust_parse_payload  # 第二階段（完整清單）也用寬鬆讀取
    except Exception:
        pass
    _FULL_PARSER = True


補查沒價格的航班 = False   # Google 清單上沒價格的航班（例如易斯達），點進去補查來回價
補查上限 = 3              # 每次查詢最多補查幾班，避免太慢


def bundled_returns(rq):
    """選定去程後，只用「來回組合價」的方式抓回程；抓不到就回傳空的（不改用單程價）"""
    from fast_flights import fetcher
    leg = fetcher._get_expected_return_leg(rq)
    if leg is None:
        return []
    r = fetcher._get_selected_html_results(rq, leg)
    if r is None:
        r = fetcher._get_bundled_leg_results(rq)
    return r or []


補抓完整清單 = False      # 除了第一次載入的航班，再向 Google 要一次完整清單（找到更多航空，但每組日期多查 1 次）


def _key(f):
    try:
        return (tuple(f.airlines or []), tuple(hm(f.flights[0].departure.time)), f.flights[0].from_airport.code)
    except Exception:
        return None


# ── 備用來源：SerpApi（被 Google 擋住時才用）──
SERP_USED = 0          # 這次執行用了幾次 SerpApi
EMPTY_CONFIRMED = 0    # 這次執行確認「真的沒航班」的天數
SERP_MAX = int(os.environ.get("SERPAPI_MAX", "40"))  # 每次執行最多用幾次，避免一次用光每月額度


def serpapi_flights(q):
    """用 SerpApi 查同一組條件，轉成跟原本一樣的格式。沒有金鑰、超過上限或失敗都回傳 None"""
    global SERP_USED
    key = os.environ.get("SERPAPI_KEY", "").strip()
    if not key or SERP_USED >= SERP_MAX:
        return None
    import urllib.parse
    import urllib.request
    from fast_flights.model import Airport, CarbonEmission, Flights, SimpleDatetime, SingleFlight
    try:
        legs = q._flights
        frm, to = legs[0].from_airport, legs[0].to_airport
        d1, d2 = str(legs[0].date)[:10], str(legs[1].date)[:10]
    except Exception:
        return None
    params = {"engine": "google_flights", "departure_id": frm, "arrival_id": to,
              "outbound_date": d1, "return_date": d2, "type": "1",
              "currency": "TWD", "hl": "zh-TW", "gl": "tw", "adults": str(大人人數),
              "stops": {0: "1", 1: "2", 2: "3"}.get(轉機, "0"), "api_key": key}
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"      ✗ 備用來源（SerpApi）也失敗：{str(e)[:80]}")
        return None
    SERP_USED += 1
    if data.get("error"):
        print(f"      ✗ 備用來源（SerpApi）：{str(data['error'])[:80]}")
        return None

    def dt(v):
        a, b = (v or "1970-01-01 00:00").split(" ")
        y, m, d = map(int, a.split("-"))
        hh, mm = map(int, b.split(":"))
        return [y, m, d], [hh, mm]

    out = []
    for it in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        segs = []
        for sf in it.get("flights") or []:
            dd, dtm = dt(sf.get("departure_airport", {}).get("time"))
            ad, atm = dt(sf.get("arrival_airport", {}).get("time"))
            fno = (sf.get("flight_number") or "").split(" ")
            segs.append(SingleFlight(
                from_airport=Airport(code=sf.get("departure_airport", {}).get("id", ""), name=sf.get("departure_airport", {}).get("name", "")),
                to_airport=Airport(code=sf.get("arrival_airport", {}).get("id", ""), name=sf.get("arrival_airport", {}).get("name", "")),
                departure=SimpleDatetime(date=dd, time=dtm), arrival=SimpleDatetime(date=ad, time=atm),
                duration=sf.get("duration") or 0, plane_type=sf.get("airplane") or "",
                airline_code=fno[0] if fno else "", flight_number=fno[1] if len(fno) > 1 else ""))
        if not segs:
            continue
        names = list(dict.fromkeys(sf.get("airline", "") for sf in it.get("flights") or [] if sf.get("airline")))
        out.append(Flights(type="serpapi", price=it.get("price"), airlines=names, flights=segs,
                           carbon=CarbonEmission(typical_on_route=0, emission=0)))
    print(f"      ✓ 改用備用來源（SerpApi）查到 {len(out)} 班（本次第 {SERP_USED} 次）")
    return out


def get_flights_complete(q):
    """第一階段（網頁一打開的航班）＋第二階段（網頁之後再載入的完整清單，依價格排序），合併起來。
    被 Google 擋住（或全部沒價格）時，改用備用來源 SerpApi"""
    global EMPTY_CONFIRMED
    from fast_flights import get_flights

    def fetch_once():
        return list(get_flights(q) or [])

    try:
        flights = polite_call(fetch_once, strict=True)
    except 查詢失敗:
        alt = serpapi_flights(q)
        if alt is None:
            raise
        return alt
    if not any(f.price for f in flights):
        # 空的結果、或有航班但全部沒價格：常常是 Google「半擋」，不一定真的沒航班
        if EMPTY_CONFIRMED >= 2:
            return flights  # 這次已經確認過好幾天真的沒航班（冷門航線），不再浪費額度
        why = "Google 回傳空的結果" if not flights else "有航班但全部沒有價格"
        print(f"      ⚠ {why}（可能被半擋），15 秒後重查一次…")
        nap(15)
        again = polite_call(fetch_once) or []
        if any(f.price for f in again):
            return again
        alt = serpapi_flights(q)
        if alt is None:
            raise 查詢失敗(why)
        if not any(f.price for f in alt):
            EMPTY_CONFIRMED += 1
            print("      ℹ 備用來源也說沒有航班，確定這天真的沒有")
        return alt
    if not 補抓完整清單:
        return flights
    try:
        from fast_flights import ShoppingOptions
    except ImportError:
        from fast_flights.shopping_options import ShoppingOptions
    more = polite_call(lambda: get_flights(q, shopping=ShoppingOptions(ranking_mode="cheapest", result_sort="price"))) or []
    diag = getattr(more, "diagnostics", None)
    st = getattr(diag, "status", None)
    names2 = sorted({a for f in more for a in (f.airlines or [])})
    print(f"      第一階段 {len(flights)} 班；第二階段 {len(more)} 班" + (f"（狀態：{st}）" if st and st != "ok" else "")
          + (f"，航空：{'、'.join(names2)}" if names2 else ""))
    index = {_key(f): f for f in flights}
    added = 0
    for f in more:
        k = _key(f)
        if k is None:
            continue
        if k in index:
            if not index[k].price and f.price:
                index[k].price = f.price
        else:
            index[k] = f
            flights.append(f)
            added += 1
    if added:
        print(f"      ＋ 完整清單多找到 {added} 班")
    return flights


def click_flight_returns(rq):
    """模仿在網頁上點選一班去程：用 Google 網頁內部的查詢方式，要「選了這班之後」的回程和來回價"""
    from fast_flights import get_return_flights
    try:
        from fast_flights import ShoppingOptions
    except ImportError:
        from fast_flights.shopping_options import ShoppingOptions
    try:
        return get_return_flights(rq, shopping=ShoppingOptions(ranking_mode="cheapest", result_sort="price")) or []
    except ValueError:
        return []


def oneway_query(frm, to, d):
    from fast_flights import FlightQuery, Passengers, create_query
    return create_query(
        flights=[FlightQuery(date=d.isoformat(), from_airport=frm, to_airport=to, airlines=只看這些航空公司 or None)],
        trip="one-way", passengers=Passengers(adults=大人人數), language="zh-TW", currency="TWD", max_stops=轉機)


def _code(f):
    try:
        return f.flights[0].airline_code or ""
    except Exception:
        return ""


# ── 方法 B：開瀏覽器讀 Google Flights 網頁上顯示的價格 ──
用瀏覽器補價格 = False

# 網頁用英文讀比較穩定，所以需要航空公司的英文名
AIRLINE_EN = {
    "ZE": "Eastar Jet", "LJ": "Jin Air", "BX": "Air Busan", "7C": "Jeju Air", "TW": "T'way",
    "RF": "Aero K", "YP": "Air Premia", "KE": "Korean Air", "OZ": "Asiana",
    "IT": "Tigerair Taiwan", "CI": "China Airlines", "BR": "EVA Air", "JX": "STARLUX", "B7": "UNI Air",
    "AE": "Mandarin", "CX": "Cathay Pacific", "HX": "Hong Kong Airlines", "UO": "HK Express",
    "NX": "Air Macau", "MM": "Peach", "GK": "Jetstar Japan", "3K": "Jetstar Asia", "JQ": "Jetstar",
    "TR": "Scoot", "NH": "ANA", "JL": "Japan Airlines", "AK": "AirAsia", "D7": "AirAsia X",
    "FD": "Thai AirAsia", "Z2": "Philippines AirAsia", "VJ": "VietJet", "VN": "Vietnam Airlines",
    "5J": "Cebu Pacific", "PR": "Philippine Airlines", "SL": "Thai Lion Air", "TG": "Thai Airways",
    "SQ": "Singapore Airlines", "MH": "Malaysia Airlines",
}


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")
DEBUG_DIR = "."  # 讀不到時，把網頁截圖和文字存在這裡，方便除錯


def _parse_time_text(txt):
    """從文字找出第一個出發時間，支援英文（2:50 AM）和中文（凌晨2:50、下午5:40）"""
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)", txt)
    if m:
        return int(m.group(1)) % 12 + (12 if m.group(3) == "PM" else 0), int(m.group(2))
    m = re.search(r"(凌晨|清晨|早上|上午|中午|下午|傍晚|晚上)\s*(\d{1,2}):(\d{2})", txt)
    if m:
        h = int(m.group(2))
        if m.group(1) in ("下午", "傍晚", "晚上") and h < 12:
            h += 12
        if m.group(1) in ("凌晨", "清晨") and h == 12:
            h = 0
        return h, int(m.group(3))
    return None


def _parse_rows(labels, texts):
    out = []
    # 方式 1：無障礙說明（aria-label），英文網頁的固定格式
    for lab in labels or []:
        lab = lab or ""
        m = re.search(r"From ([\d,]+)\D{0,40}?round trip total", lab)
        t = re.search(r"[Ll]eaves .*? at (\d{1,2}):(\d{2})\s*([AP]M)", lab)
        if m and t:
            h = int(t.group(1)) % 12 + (12 if t.group(3) == "PM" else 0)
            al = re.search(r"flights? with ([^.]+)\.", lab)
            out.append({"price": int(m.group(1).replace(",", "")), "hm": (h, int(t.group(2))),
                        "airline": al.group(1) if al else "", "text": lab})
    if out:
        return out
    # 方式 2：每一班航班那一列畫面上的文字（時間、航空公司、價格）
    for txt in texts or []:
        txt = (txt or "").replace("\u202f", " ").replace("\xa0", " ")
        pm = re.findall(r"(?:NT\$|TWD|\$)\s?([\d,]{3,})", txt)
        t = _parse_time_text(txt)
        if pm and t:
            out.append({"price": int(pm[-1].replace(",", "")), "hm": t, "airline": txt, "text": txt})
    return out


def browser_read_prices(url):
    """在背景開 Edge（沒有就用 Chrome），打開 Google Flights 網頁，讀出每班航班的「來回總價」"""
    from playwright.sync_api import sync_playwright
    labels, texts = [], []
    with sync_playwright() as p:
        browser = None
        args = ["--disable-blink-features=AutomationControlled"]
        for ch in ("msedge", "chrome", None):
            try:
                browser = (p.chromium.launch(channel=ch, headless=True, args=args) if ch
                           else p.chromium.launch(headless=True, args=args))
                break
            except Exception:
                continue
        if browser is None:
            raise RuntimeError("找不到可以用的瀏覽器（Edge 或 Chrome）")
        try:
            ctx = browser.new_context(locale="en-US", user_agent=BROWSER_UA,
                                      viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_selector("li", timeout=20000)
            except Exception:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(3000)  # 等網頁把價格補完
            try:  # 有「更多航班」就展開
                page.get_by_role("button", name=re.compile(r"more flights|更多航班", re.I)).first.click(timeout=2000)
                page.wait_for_timeout(3000)
            except Exception:
                pass
            for _ in range(4):  # 往下捲，讓下面的航班也載入
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(600)
            labels = page.eval_on_selector_all("[aria-label]", "els => els.map(e => e.getAttribute('aria-label'))")
            texts = page.eval_on_selector_all("li", "els => els.map(e => e.innerText)")
            rows = _parse_rows(labels, texts)
            if not rows:  # 讀不到：存下截圖和文字，方便除錯
                try:
                    page.screenshot(path=os.path.join(DEBUG_DIR, "瀏覽器除錯.png"), full_page=True)
                    with open(os.path.join(DEBUG_DIR, "瀏覽器除錯.txt"), "w", encoding="utf-8") as fh:
                        fh.write("網址：" + url + "\n標題：" + page.title() + "\n\n")
                        fh.write("=== 網頁文字（前 6000 字）===\n" + page.inner_text("body")[:6000] + "\n\n")
                        fh.write("=== aria-label（前 60 個）===\n" + "\n".join(l for l in labels[:60] if l))
                    print("      （已把網頁截圖存成「瀏覽器除錯.png」，文字存成「瀏覽器除錯.txt」）")
                except Exception:
                    pass
        finally:
            browser.close()
    return rows


def fill_by_browser(q, missing):
    """方法 B：沒價格的航班，到網頁上找同一個出發時間、同一家航空的價格"""
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("      （沒有安裝瀏覽器工具 playwright，跳過開瀏覽器補價格）")
        return
    try:
        legs = q._flights
        d1 = date.fromisoformat(str(legs[0].date)[:10])
        d2 = date.fromisoformat(str(legs[1].date)[:10])
        url = make_query(d1, d2, language="en-US").url()
    except Exception:
        return
    names = "、".join(dict.fromkeys(" / ".join(f.airlines or []) or "未知航空" for f in missing))
    print(f"    · 補查價格（開瀏覽器讀網頁）：{names}")
    if should_stop():
        raise 已停止()
    try:
        rows = browser_read_prices(url)
    except Exception as e:
        print(f"      ✗ 瀏覽器開啟失敗：{str(e)[:80]}")
        return
    if not rows:
        print("      ✗ 網頁上讀不到航班價格")
        return
    for f in missing:
        t = hm(f.flights[0].departure.time)
        en = AIRLINE_EN.get(_code(f), "").lower()
        zh = set(f.airlines or []) | {AIRLINE_NAMES.get(_code(f), "")}
        cands = [r for r in rows if r["hm"] == t]
        named = [r for r in cands if (en and en in r["airline"].lower()) or any(z and z in r["airline"] for z in zh)]
        pick = named or (cands if len({r["airline"] for r in cands}) == 1 else [])
        label = " / ".join(f.airlines or []) or "未知航空"
        if pick:
            f.price = min(r["price"] for r in pick)
            print(f"      ✓ {label} {fmt_time(f.flights[0].departure.time)}：NT${f.price:,}（網頁上的價格）")
        else:
            print(f"      ✗ {label} {fmt_time(f.flights[0].departure.time)}：網頁上找不到這班")


def fill_missing_prices(q, flights):
    """沒價格的航班（例如 Google 資料不完整的易斯達）：
    先開瀏覽器讀網頁上的價格；讀不到的，再用去程、回程單程相加"""
    if not 補查沒價格的航班:
        return flights
    missing = [f for f in flights if not f.price and f.flights
               and in_hours(hm(f.flights[0].departure.time)[0], 去程最早出發, 去程最晚出發)]
    if not missing:
        return flights
    if 用瀏覽器補價格:
        fill_by_browser(q, missing)
        missing = [f for f in missing if not f.price]
        if not missing:
            return flights
    try:
        legs = q._flights
        frm, to = legs[0].from_airport, legs[0].to_airport
        d1, d2 = date.fromisoformat(str(legs[0].date)[:10]), date.fromisoformat(str(legs[1].date)[:10])
    except Exception:
        return flights
    names = "、".join(dict.fromkeys(" / ".join(f.airlines or []) or "未知航空" for f in missing))
    print(f"    · 補查價格（單程相加）：{names}")
    outs = get_flights_complete(oneway_query(frm, to, d1))
    rets = get_flights_complete(oneway_query(to, frm, d2))
    for f in missing:
        code, name, t = _code(f), set(f.airlines or []), hm(f.flights[0].departure.time)
        same = lambda g: (code and _code(g) == code) or (name and name & set(g.airlines or []))
        o = [g for g in outs if g.price and same(g) and hm(g.flights[0].departure.time) == t]
        r = [g for g in rets if g.price and same(g) and in_hours(hm(g.flights[0].departure.time)[0], 回程最早出發, 回程最晚出發)]
        label = " / ".join(f.airlines or []) or "未知航空"
        if o and r:
            ob, rb = min(o, key=lambda g: g.price), min(r, key=lambda g: g.price)
            f.price = int(ob.price) + int(rb.price)
            f.ow_sum = True
            f.ow_ret = describe(rb)
            if f.ow_ret:
                f.ow_ret["price"] = f.price
            print(f"      ✓ {label} {fmt_time(f.flights[0].departure.time)}：去程 NT${int(ob.price):,} ＋ 回程 NT${int(rb.price):,} ＝ NT${f.price:,}")
        else:
            why = "單程清單也沒有去程價格" if not o else "找不到同一家的回程價格"
            print(f"      ✗ {label} {fmt_time(f.flights[0].departure.time)}：{why}")
    return flights


def search_pair_live(d1, d2):
    from fast_flights import get_flights

    use_full_results()

    q = make_query(d1, d2)
    print("    · 查去程（含來回價）")
    outs = [o for o in (describe(f) for f in fill_missing_prices(q, get_flights_complete(q))) if o]
    outs = [o for o in outs if in_hours(o["hour"], 去程最早出發, 去程最晚出發)]
    outs = by_airline(outs)

    rets, reliable, picked, combos = {}, {}, {}, []
    for airline, lst in outs.items():
        priced = [x for x in lst if x["price"] is not None]
        if not priced:
            continue  # 這家只有沒價格的航班：列在表格裡，但不查回程
        cheapest = min(priced, key=pv)
        targets = priced if 完整組合 else [cheapest]
        for o in targets:
            ow_ret = getattr(o.get("obj"), "ow_ret", None)
            if ow_ret:
                r, ok = [ow_ret], True   # 單程相加的航班：回程用單程查到的那班
            else:
                print(f"    · 查回程：{airline} {o['text']}")
                r, ok = fetch_returns(q, o)
            for x in r:
                combos.append((airline, o["text"], x["text"], x["price"], ok))
            if o is cheapest:
                rets[airline], reliable[airline], picked[airline] = r, ok, o["text"]
    return outs, rets, reliable, picked, combos, q.url()


# ─────────────────────────── 假資料 ───────────────────────────
def search_pair_demo(d1, d2):
    rnd = random.Random(f"{d1}{d2}")
    table = {
        "國泰航空": [(8, 20, 130), (9, 50, 135), (10, 45, 135), (12, 25, 125)],
        "中華航空": [(7, 40, 130), (13, 0, 130), (16, 35, 125)],
        "長榮航空": [(9, 15, 125), (14, 5, 130)],
        "香港航空": [(11, 50, 130), (16, 35, 125)],
        "星宇航空": [(8, 0, 130), (19, 45, 115)],
    }
    base = {"香港航空": 5559, "星宇航空": 7270, "國泰航空": 7158, "中華航空": 6880, "長榮航空": 7020}

    def mk(airline, h, m, dur, price):
        arr = h * 60 + m + dur
        return {"airline": airline, "text": f"{h:02d}:{m:02d} - {arr // 60 % 24:02d}:{arr % 60:02d}",
                "price": price, "key": h * 60 + m, "hour": h}

    outs, rets, reliable, picked, combos = {}, {}, {}, {}, []
    for a, slots in table.items():
        o = [mk(a, h, m, d, base[a] + rnd.choice([0, 0, 250, 250, 600])) for h, m, d in slots if rnd.random() > 0.1]
        if not o:
            continue
        outs[a] = sorted(o, key=lambda x: x["key"])
        cheapest = min(o, key=lambda x: x["price"])
        r = [mk(a, (h + 5) % 24, m, d, cheapest["price"] + rnd.choice([0, 0, 250, 1019]))
             for h, m, d in slots]
        r[0]["price"] = cheapest["price"]
        rets[a], reliable[a], picked[a] = sorted(r, key=lambda x: x["key"]), True, cheapest["text"]
        for oo in (o if 完整組合 else [cheapest]):
            for rr in r:
                combos.append((a, oo["text"], rr["text"], rr["price"] - cheapest["price"] + oo["price"], True))
    return outs, rets, reliable, picked, combos, "https://www.google.com/travel/flights"


# ─────────────────────────── 月份區間掃描 ───────────────────────────
def scan_day_live(d1, d2):
    """每個出發日只查 1 次：拿到每家航空的最低來回價（回程細節之後再補）"""
    from fast_flights import get_flights

    use_full_results()
    q = make_query(d1, d2)
    outs = [o for o in (describe(f) for f in fill_missing_prices(q, get_flights_complete(q))) if o]
    outs = [o for o in outs if in_hours(o["hour"], 去程最早出發, 去程最晚出發)]
    return {"q": q, "url": q.url()}, by_airline(outs)


def scan_detail_live(ctx, out_item):
    """補查某一筆的回程時間"""
    return fetch_returns(ctx["q"], out_item)


def scan_day_demo(d1, d2):
    outs, rets, reliable, picked, combos, url = search_pair_demo(d1, d2)
    return {"url": url, "rets": rets}, outs


def scan_detail_demo(ctx, out_item):
    return ctx["rets"].get(out_item["airline"], []), True


# ─────────────────────────── Excel ───────────────────────────
FONT = "微軟正黑體"
BLUE = PatternFill("solid", start_color="CFE2F3")
GOLD = PatternFill("solid", start_color="FFE599")
HEAD = PatternFill("solid", start_color="EFEFEF")
thin = Side(style="thin", color="BFBFBF")
thick = Side(style="medium", color="000000")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
PRICE_FMT = '"NT$"#,##0'
STOP_TXT = {0: "直飛", 1: "最多轉 1 次", None: "可轉機"}.get(轉機, f"最多轉 {轉機} 次")


def style_header(ws, last_col):
    for r in (1, 2):
        for c in range(1, last_col + 1):
            cell = ws.cell(r, c)
            cell.font = Font(name=FONT, bold=True, size=10)
            cell.alignment = CENTER
            cell.fill = HEAD
            cell.border = Border(left=thin, right=thin, top=thin, bottom=thick if r == 2 else thin)


def write_main_sheet(ws, results, pairs):
    def best(a):
        vals = [min(pv(x) for x in res[0][a]) for res in results if res[0].get(a)]
        vals = [v for v in vals if v != INF]
        return min(vals) if vals else 10**9

    airlines = sorted({a for res in results for a in res[0]}, key=best)
    if not airlines:
        ws["A1"] = "查無航班，請確認機場代碼、日期，或放寬轉機條件。"
        return

    ws["A1"] = f"{出發地} ⇄ {目的地}"
    ws.merge_cells("A1:A2")
    sub = ["去程", "來回價", "回程", "來回價", "來回最低"]
    for i, a in enumerate(airlines):
        c0 = 2 + i * 5
        ws.cell(1, c0, f"{a}（{STOP_TXT}）")
        ws.merge_cells(start_row=1, start_column=c0, end_row=1, end_column=c0 + 4)
        for j, s in enumerate(sub):
            ws.cell(2, c0 + j, s)
    last_col = 1 + len(airlines) * 5
    style_header(ws, last_col)

    row = 3
    for (d1, d2), (outs, rets, reliable, picked, _, url) in zip(pairs, results):
        n = max([len(outs.get(a, [])) for a in airlines] + [len(rets.get(a, [])) for a in airlines] + [1])
        top, bot = row, row + n - 1

        c = ws.cell(top, 1, f"{d1.month}/{d1.day}({週[d1.weekday()]})\n～{d2.month}/{d2.day}({週[d2.weekday()]})")
        c.hyperlink = url
        # 每列至少 20 點高；只有一列時加高，讓兩行日期放得下
        for rr in range(top, bot + 1):
            ws.row_dimensions[rr].height = 20 if n > 1 else 36
        ws.merge_cells(start_row=top, start_column=1, end_row=bot, end_column=1)

        totals = {}
        for i, a in enumerate(airlines):
            c0 = 2 + i * 5
            o, r = outs.get(a, []), rets.get(a, [])
            for k, it in enumerate(o):
                star = it["text"] == picked.get(a)
                ws.cell(top + k, c0, ("★ " if star else "") + it["text"])
                ws.cell(top + k, c0 + 1, it["price"] if it["price"] is not None else "未提供")
            for k, it in enumerate(r):
                ws.cell(top + k, c0 + 2, it["text"])
                ws.cell(top + k, c0 + 3, it["price"] if it["price"] is not None else "未提供")
            for col, lst in ((c0 + 1, o), (c0 + 3, r)):
                if any(x["price"] is not None for x in lst):
                    m = min(pv(x) for x in lst)
                    for k, x in enumerate(lst):
                        if x["price"] == m:
                            ws.cell(top + k, col).fill = BLUE
            if r and not reliable.get(a, True):
                ws.cell(top, c0 + 3).comment = Comment(
                    "Google 這次沒有回傳來回組合價，這一欄是「單程價」。請點左邊日期到 Google Flights 確認。", "查機票")
            L = get_column_letter(c0 + 1)
            ws.cell(top, c0 + 4, f'=IF(COUNT({L}{top}:{L}{bot})=0,"",MIN({L}{top}:{L}{bot}))')
            ws.merge_cells(start_row=top, start_column=c0 + 4, end_row=bot, end_column=c0 + 4)
            if o:
                if any(x["price"] is not None for x in o):
                    totals[c0 + 4] = min(pv(x) for x in o)

        if totals:
            m = min(totals.values())
            for col, v in totals.items():
                if v == m:
                    ws.cell(top, col).fill = GOLD

        for rr in range(top, bot + 1):
            for cc in range(1, last_col + 1):
                cell = ws.cell(rr, cc)
                pos = (cc - 2) % 5
                unreliable = cc > 1 and pos == 3 and not reliable.get(airlines[(cc - 2) // 5], True)
                cell.font = Font(name=FONT, size=10,
                                 bold=(cc > 1 and pos == 4),
                                 italic=unreliable, color="999999" if unreliable else ("0563C1" if cc == 1 else None),
                                 underline="single" if cc == 1 else None)
                cell.alignment = CENTER
                if cc > 1 and pos in (1, 3, 4):
                    cell.number_format = PRICE_FMT
                left = thick if cc == 1 or pos == 0 else thin
                cell.border = Border(left=left, right=thin, top=thin, bottom=thick if rr == bot else thin)
        row = bot + 1

    ws.column_dimensions["A"].width = 18
    tw = 26 if 轉機 != 0 else 17
    for i in range(len(airlines)):
        for j, w in enumerate((tw, 13, tw, 13, 14)):
            ws.column_dimensions[get_column_letter(2 + i * 5 + j)].width = w
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "B3"


def write_combo_sheet(ws, results, pairs):
    heads = ["日期", "航空公司", "去程", "回程", "來回價"]
    for j, h in enumerate(heads, 1):
        c = ws.cell(1, j, h)
        c.font = Font(name=FONT, bold=True)
        c.fill = HEAD
        c.alignment = CENTER
    r = 2
    for (d1, d2), res in zip(pairs, results):
        combos = sorted(res[4], key=lambda x: x[3])
        if not combos:
            continue
        lowest = combos[0][3]
        for airline, ot, rt, price, ok in combos:
            vals = [date_label(d1, d2).replace("\n", ""), airline, ot, rt, price]
            for j, v in enumerate(vals, 1):
                c = ws.cell(r, j, v)
                c.font = Font(name=FONT, size=10, italic=not ok, color=None if ok else "999999")
                c.alignment = CENTER
            ws.cell(r, 5).number_format = PRICE_FMT
            if price == lowest:
                ws.cell(r, 5).fill = GOLD
            r += 1
    for j, w in enumerate((22, 18, 26, 26, 14), 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:E{max(r - 1, 1)}"


def write_scan_excel(top, calendar, filename):
    wb = Workbook()
    ws = wb.active
    ws.title = "最便宜前10名"
    heads = ["名次", "去程日", "回程日", "航空公司", "去程時間", "回程時間", "來回價"]
    for j, h in enumerate(heads, 1):
        c = ws.cell(1, j, h)
        c.font = Font(name=FONT, bold=True, size=10)
        c.fill = HEAD
        c.alignment = CENTER
        c.border = Border(bottom=thick)
    for i, t in enumerate(top, 2):
        vals = [i - 1, t["d1label"], t["d2label"], t["airline"], t["out"], t["ret"] or "（未查到）", t["price"]]
        for j, v in enumerate(vals, 1):
            c = ws.cell(i, j, v)
            c.font = Font(name=FONT, size=10, bold=(j == 7))
            c.alignment = CENTER
            c.border = Border(bottom=thin)
        ws.cell(i, 7).number_format = PRICE_FMT
        ws.cell(i, 2).hyperlink = t["url"]
        ws.cell(i, 2).font = Font(name=FONT, size=10, color="0563C1", underline="single")
        if i == 2:
            ws.cell(i, 7).fill = GOLD
    for j, w in enumerate((7, 14, 14, 16, 22, 22, 13), 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("每天最低價")
    heads = ["出發日", "回程日", "最低來回價", "航空公司"]
    for j, h in enumerate(heads, 1):
        c = ws2.cell(1, j, h)
        c.font = Font(name=FONT, bold=True, size=10)
        c.fill = HEAD
        c.alignment = CENTER
    low = min((c["price"] for c in calendar if c["price"]), default=None)
    for i, c in enumerate(calendar, 2):
        vals = [c["d1label"], c["d2label"], c["price"], c["airline"]]
        for j, v in enumerate(vals, 1):
            cell = ws2.cell(i, j, v)
            cell.font = Font(name=FONT, size=10)
            cell.alignment = CENTER
        ws2.cell(i, 3).number_format = PRICE_FMT
        if c["price"] is not None and c["price"] == low:
            ws2.cell(i, 3).fill = GOLD
    for j, w in enumerate((14, 14, 13, 18), 1):
        ws2.column_dimensions[get_column_letter(j)].width = w
    ws2.freeze_panes = "A2"
    ws2.auto_filter.ref = f"A1:D{max(len(calendar) + 1, 1)}"
    write_info_sheet(wb.create_sheet("說明"))
    wb.save(filename)


def write_info_sheet(ws):
    lines = [
        ("查詢時間", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("航線", f"{出發地} ⇄ {目的地}"),
        ("轉機", STOP_TXT),
        ("人數", f"大人 {大人人數} 位（價格為全部人數合計）"),
        ("", ""),
        ("怎麼看", "所有價格都是 Google Flights 的「來回票」總價，不是單程相加。"),
        ("", "去程旁的來回價 = 選這班去程時，最便宜的來回價。"),
        ("", "回程旁的來回價 = 搭配標 ★ 的那班去程（該航空當天最便宜的去程）時的來回價。"),
        ("", "來回最低 = 這家航空這組日期最便宜的來回價。"),
        ("", "藍底 = 該欄最便宜；金底 = 同一組日期所有航空公司中最便宜。"),
        ("", "灰色斜體 = Google 這次沒給來回組合，顯示的是單程價，請自行確認。"),
        ("", "時間後面 +1 = 隔天抵達；（經 XXX）= 在該機場轉機。"),
        ("", "點左邊日期可直接開 Google Flights 那組來回搜尋。"),
        ("", "開啟「完整組合」時，「所有組合」分頁會列出每班去程 × 每班回程的來回價，可用篩選。"),
        ("行李", "Google 顯示的是基本票價，託運行李是否包含請以航空公司為準（傳統航空通常含）。"),
    ]
    if NOTES:
        lines = [("⚠ 注意", "以下航空公司查不到價格，沒有列進比價表，請到 Google Flights 查看：")] + \
                [("", "‧ " + n.split("：")[0]) for n in dict.fromkeys(NOTES)] + [("", "")] + lines
    for r, (k, v) in enumerate(lines, 1):
        ws.cell(r, 1, k).font = Font(name=FONT, bold=True, color="C62828" if k.startswith("⚠") else None)
        ws.cell(r, 2, v).font = Font(name=FONT, color="C62828" if (k.startswith("⚠") or v.startswith("‧")) else None)
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 90


def write_excel(results, pairs, filename):
    wb = Workbook()
    ws = wb.active
    ws.title = "比價"
    write_main_sheet(ws, results, pairs)
    if 完整組合:
        write_combo_sheet(wb.create_sheet("所有組合"), results, pairs)
    write_info_sheet(wb.create_sheet("說明"))
    wb.save(filename)


# ─────────────────────────── 主程式 ───────────────────────────
def main():
    demo = "--demo" in sys.argv
    if not demo:
        try:
            from fast_flights import select_flight, get_return_flights  # noqa: F401
        except ImportError:
            print("套件版本不對或沒安裝。請在終端機依序執行：")
            print("  pip uninstall -y fast-flights")
            print("  pip install --force-reinstall faster-flights")
            sys.exit(1)

    pairs = build_date_pairs()
    if not pairs:
        print("沒有任何日期，請檢查設定區。")
        return

    mode = "完整組合" if 完整組合 else "快速"
    print(f"✈ {出發地} ⇄ {目的地}，{len(pairs)} 組日期，{mode}模式" + ("（示範，假資料）" if demo else ""))
    results = []
    for idx, (d1, d2) in enumerate(pairs, 1):
        print(f"[{idx}/{len(pairs)}] {d1} ~ {d2}", flush=True)
        results.append(search_pair_demo(d1, d2) if demo else search_pair_live(d1, d2))

    name = ("示範_" + 輸出檔名) if demo else 輸出檔名
    try:
        write_excel(results, pairs, name)
    except PermissionError:
        print(f"✗ 無法存檔：{name} 可能正開著，請先關掉 Excel 再跑一次。")
        return
    print(f"✓ 完成！已存成 {name}")


if __name__ == "__main__":
    main()
