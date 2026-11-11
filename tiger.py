# -*- coding: utf-8 -*-
"""
台灣虎航官網的「每日最低價」資料（官網票價月曆用的公開資料網址）。
一次抓一個台灣出發站，就有那個站所有航線、每一天的最低價（單人、單程、未稅起價）。
參考：github.com/jhunterxsaxon/tigerair-price 的研究。
禮貌使用：每次執行最多抓 4 站，一天合計不到 30 次。
"""
import gzip
import io
import json
import time
import urllib.request

API = "https://api-book.tigerairtw.com/api/cms/station-daily-prices/{station}/TWD"
TW_STATIONS = ["TPE", "KHH", "RMQ", "TNN"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
           "referer": "https://www.tigerairtw.com/", "Accept": "application/json", "Accept-Encoding": "gzip"}
NAMES = {"TPE": "台北", "KHH": "高雄", "RMQ": "台中", "TNN": "台南", "NRT": "東京成田", "HND": "東京羽田", "KIX": "大阪",
         "OKJ": "岡山", "YGJ": "米子", "KCZ": "高知", "FUK": "福岡", "HSG": "佐賀", "KMI": "宮崎", "KMJ": "熊本", "OIT": "大分",
         "CTS": "札幌", "HKD": "函館", "SDJ": "仙台", "AXT": "秋田", "FKS": "福島", "HNA": "花卷", "KIJ": "新潟", "NGO": "名古屋",
         "KMQ": "小松", "OKA": "沖繩", "ISG": "石垣島", "ICN": "首爾仁川", "GMP": "首爾金浦", "PUS": "釜山", "CJU": "濟州",
         "DAD": "峴港", "HKT": "普吉島", "BKK": "曼谷", "MFM": "澳門"}

_cache = {}
LAST_ERROR = ""


def _fetch(station):
    """抓一站，回傳原始資料列；失敗回傳 None"""
    global LAST_ERROR
    if station in _cache:
        return _cache[station]
    for attempt in range(3):
        try:
            req = urllib.request.Request(API.format(station=station), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            data = json.loads(raw.decode("utf-8"))
            rows = data if isinstance(data, list) else (data.get("data") or [])
            if rows:
                _cache[station] = rows
                time.sleep(2)  # 站與站之間休息一下
                return rows
            LAST_ERROR = "回應裡沒有資料"
        except Exception as e:
            LAST_ERROR = f"{type(e).__name__}: {str(e)[:60]}"
        time.sleep(3 * (attempt + 1))
    _cache[station] = None
    return None


def daily(origin, dest):
    """origin → dest 每一天的最低價 {'2027-03-01': 1799, ...}；虎航沒飛或抓不到回傳 {}"""
    station = origin if origin in TW_STATIONS else dest if dest in TW_STATIONS else None
    if not station:
        return {}
    rows = _fetch(station) or []
    best = {}
    for r in rows:
        if r.get("origin") != origin or r.get("destination") != dest:
            continue
        try:
            amt = int(float(r.get("pricingAmount") or 0))
        except (TypeError, ValueError):
            continue
        day = (r.get("pricingDate") or "")[:10]
        if amt <= 0 or len(day) != 10:
            continue
        upd = r.get("updatedAt") or ""
        old = best.get(day)
        if old is None or upd > old[1]:  # 同一天有重複資料時，用最新更新的那筆
            best[day] = (amt, upd)
    return {k: v[0] for k, v in best.items()}


def ok(origin, dest):
    """這次有沒有成功抓到資料（不是「虎航沒飛」）"""
    station = origin if origin in TW_STATIONS else dest if dest in TW_STATIONS else None
    return bool(station) and _cache.get(station) is not None


def round_trip(origin, dest, pairs):
    """多組 (去程日, 回程日) 裡，未稅來回起價最便宜的一組：(總價, 去程日, 回程日, 去程價, 回程價)；沒有回傳 None"""
    out, back = daily(origin, dest), daily(dest, origin)
    best = None
    for d1, d2 in pairs:
        a, b = out.get(d1.isoformat()), back.get(d2.isoformat())
        if a and b and (best is None or a + b < best[0]):
            best = (a + b, d1, d2, a, b)
    return best


COUNTRY = {"NRT": "日本", "HND": "日本", "KIX": "日本", "OKJ": "日本", "YGJ": "日本", "KCZ": "日本", "FUK": "日本",
           "HSG": "日本", "KMI": "日本", "KMJ": "日本", "OIT": "日本", "CTS": "日本", "HKD": "日本", "SDJ": "日本",
           "AXT": "日本", "FKS": "日本", "HNA": "日本", "KIJ": "日本", "NGO": "日本", "KMQ": "日本", "OKA": "日本",
           "ISG": "日本", "ICN": "韓國", "GMP": "韓國", "PUS": "韓國", "CJU": "韓國", "DAD": "越南", "HKT": "泰國",
           "BKK": "泰國", "MFM": "澳門"}


def name(code):
    """台北（桃園）、日本（福岡）這種寫法"""
    if code == "TPE":
        return "台北（桃園）"
    city = NAMES.get(code, code)
    c = COUNTRY.get(code)
    if not c:
        return city
    if c == "澳門":
        return "澳門"
    return f"{c}（{city}）"
