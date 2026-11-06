# -*- coding: utf-8 -*-
"""
特價快報：定時搜尋廉航促銷新聞，有新的就傳 Telegram 通知。
在 GitHub Actions 上自動執行，不需要自己的電腦開著。

需要在 GitHub 設定兩個 Secrets：
  TELEGRAM_TOKEN    Telegram 機器人的 token
  TELEGRAM_CHAT_ID  你的 Telegram 聊天 ID
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
KEYWORD_FILE = os.path.join(HERE, "關鍵字.txt")
SEEN_FILE = os.path.join(HERE, "seen.json")
REMIND_FILE = os.path.join(HERE, "reminders.json")
REMIND_BEFORE = 30    # 開搶前幾分鐘提醒
SEARCH_EVERY_HOURS = 2  # 每幾小時搜尋一次新聞（提醒檢查則是每次執行都做）
TW = timezone(timedelta(hours=8))

# 標題裡至少要有其中一個字，才算是「促銷消息」，避免一般航空新聞也被通知
PROMO_WORDS = ["促銷", "特價", "優惠", "開賣", "開搶", "搶票", "元起", "折", "限時", "快閃",
               "買一送一", "免費", "0元", "1元", "週年慶", "周年慶", "降價", "便宜",
               "旅展", "下殺", "破盤", "超狂", "優惠價", "搶便宜", "未稅", "限量", "秒殺"]
# 標題裡也要有其中一個跟「飛機」有關的字，避免電信、百貨的促銷也被通知
FLIGHT_WORDS = ["機票", "航空", "航班", "班機", "機位", "廉航", "航線", "飛", "直航", "來回", "單程",
                "虎航", "樂桃", "酷航", "捷星", "亞航", "越捷", "易斯達", "濟州", "真航空", "德威",
                "釜山航", "香港快運", "獅航", "宿霧", "星宇", "長榮", "華航", "華信", "立榮", "國泰"]
MAX_AGE_DAYS = 3      # 只看最近幾天的新聞
MAX_DAYS_TXT = f"{MAX_AGE_DAYS} 天"
KEEP_SEEN = 2000      # 最多記住幾則已通知過的


def log(*a):
    print(*a, flush=True)


def load_keywords():
    words = []
    with open(KEYWORD_FILE, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)
    return words


def fetch(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "Mozilla/5.0 (deal-watch; personal use)",
        **({"Content-Type": "application/json"} if data else {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def search_news(keyword):
    """用 Google 新聞的 RSS 搜尋（公開、免費）"""
    q = urllib.parse.quote(f"{keyword} when:{MAX_AGE_DAYS}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    root = ET.fromstring(fetch(url))
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        source = (it.findtext("source") or "").strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(TW)
        except Exception:
            pub = None
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        items.append({"id": guid, "title": title, "link": link, "source": source, "pub": pub, "kw": keyword})
    return items


# 推播條件：標題要「同時」有 ① 跟機票有關 ② 價格 ③ 開賣時間或開賣字眼
PRICE_RE = re.compile(r"\d+\s*萬\s*\d*\s*元|\d[\d,]*\s*元|\d{3,}[\d,]*\s*起|\d{1,2}(\.\d)?\s*折|[0-9]+\s*TWD|NT\$\s*\d")
TIME_RE = re.compile(r"(?<![\d.,])\d{1,2}\s*[點時]|\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|\d{1,2}月\d{1,2}[日號]|[今明]\s*\(?\d{0,2}\)?\s*[日天早晚]")
SALE_WORDS = ["開賣", "開搶", "搶票", "快閃", "限時", "秒殺"]
# 標題一定要有其中一家航空公司的名字（只寫「機票」的，常常是抽獎、刷卡活動）
AIRLINE_WORDS = ["虎航", "樂桃", "酷航", "捷星", "亞航", "亞洲航空", "越捷", "濟州航", "真航空", "易斯達", "德威",
                 "釜山航空", "Aero K", "香港快運", "香港航空", "國泰", "獅航", "宿霧太平洋", "星宇", "長榮", "華航",
                 "中華航空", "華信", "立榮", "大韓航空", "韓亞", "日航", "日本航空", "全日空", "ANA", "JAL",
                 "春秋航空", "吉祥航空", "泰國航空", "泰航", "越南航空", "越航", "新加坡航空", "新航", "馬航",
                 "菲律賓航空", "菲航", "阿聯酋", "卡達航空", "土耳其航空", "聯合航空", "達美", "美國航空", "加航",
                 "澳航", "紐西蘭航空", "漢莎", "法航", "荷航", "英航", "澳門航空", "Peach", "Scoot", "Jetstar",
                 "AirAsia", "Tigerair", "STARLUX", "EVA Air", "酷鳥"]
# 抽獎、信用卡、里程這類活動不推
PROMO_EXCLUDE = ["抽獎", "抽機票", "抽出", "抽中", "抽送", "抽好禮", "好禮", "聯名卡", "信用卡", "刷卡", "里程", "集點",
                 "點數", "會員日送", "留言", "分享抽", "轉發"]
# 台灣國內線（離島、東部）的字眼
DOMESTIC_WORDS = ["離島", "國內線", "澎湖", "馬公", "金門", "馬祖", "南竿", "北竿", "七美", "望安",
                  "蘭嶼", "綠島", "台東", "臺東", "花蓮", "恆春", "本島"]
# 國外地名、國際線字眼（有這些就算國際線）
INTL_WORDS = ["國際", "海外", "出國", "全航線", "日本", "日韓", "韓國", "泰國", "越南", "新加坡", "馬來西亞",
              "菲律賓", "印尼", "柬埔寨", "東南亞", "香港", "港澳", "澳門", "中國", "大陸", "美國", "加拿大",
              "歐洲", "澳洲", "紐西蘭", "東京", "大阪", "京都", "名古屋", "福岡", "沖繩", "北海道", "札幌",
              "首爾", "釜山", "濟州", "曼谷", "清邁", "普吉", "峴港", "河內", "胡志明", "富國島", "宿霧",
              "長灘島", "峇里島", "吉隆坡", "檳城", "關島", "帛琉", "塞班", "杜拜", "日本線", "韓國線"]
# 標題有這些字就不推（航空公司的股票、財報新聞也會有「元」和日期，要排除）
EXCLUDE_WORDS = ["股", "台股", "漲停", "跌停", "收盤", "盤中", "外資", "投信", "自營商", "營收", "財報", "EPS",
                 "獲利", "法說", "除息", "配息", "股利", "目標價", "市值", "本益比", "貨運", "運價", "航空貨",
                 "罷工", "訂單", "飛機引擎", "機隊", "交機", "季報", "年報", "盈餘", "虧損", "淨利"]


def is_ticket_deal(t):
    return deal_level(t) == "yes"


def deal_level(t):
    """yes＝標題就符合；maybe＝有機票和開賣時間、但標題沒寫價格（要點進去看內文）；no＝不符合"""
    if any(w in t for w in EXCLUDE_WORDS) or any(w in t for w in PROMO_EXCLUDE):
        return "no"
    # 只要國際線：有台灣國內地名、又沒有國外地名的，不推
    if any(w in t for w in DOMESTIC_WORDS) and not any(w in t for w in INTL_WORDS):
        return "no"
    flight = any(w in t for w in AIRLINE_WORDS)
    price = bool(PRICE_RE.search(t))
    when = bool(TIME_RE.search(t)) or any(w in t for w in SALE_WORDS)
    if flight and when:
        return "yes" if price else "maybe"
    return "no"


# ── 讀內文：標題沒寫價格時，點進新聞找價格 ──
MAX_DETAIL = 10   # 每次最多點進去幾篇
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


def http_get(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(800_000).decode("utf-8", "ignore"), r.geturl()


def decode_google_news(link):
    """Google 新聞的連結是加密轉址，這裡解開成真正的新聞網址；失敗回傳 None"""
    m = re.search(r"/articles/([^?/]+)", link)
    if not m:
        return None
    gid = m.group(1)
    page, _ = http_get(f"https://news.google.com/rss/articles/{gid}")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not sg or not ts:
        return None
    inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{gid}",{ts.group(1)},"{sg.group(1)}"]')
    body = urllib.parse.urlencode({"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])}).encode()
    txt, _ = http_get("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    for line in txt.splitlines():
        if line.startswith("[[") and "garturlres" in line:
            arr = json.loads(line)
            return json.loads(arr[0][2])[1]
    return None


def article_text(url):
    """讀新聞網頁的摘要和開頭幾段"""
    page, _ = http_get(url)
    # 先清掉網頁裡給程式看的隱藏資料（script、style），只留真正的文章
    page = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    parts = []
    for pat in (r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"',
                r'<meta[^>]+name="description"[^>]+content="([^"]+)"'):
        m = re.search(pat, page)
        if m:
            parts.append(html.unescape(m.group(1)))
    for p in re.findall(r"<p(?:\s[^>]*)?>(.*?)</p>", page, flags=re.S | re.I)[:15]:
        t = html.unescape(re.sub(r"<[^>]+>", "", p)).strip()
        if len(t) > 15 and "{" not in t and "@context" not in t:
            parts.append(t)
    return "\n".join(parts)


PACKAGE_WORDS = ["團費", "跟團", "團體旅遊"]  # 跟團行程不推；自由行、機加酒可以


def price_sentence(text):
    """找出有價格的那一句（最多 70 個字）；國內線、套裝行程的價格不算"""
    for sent in re.split(r"[。！\n]", text):
        if any(w in sent for w in PACKAGE_WORDS) or any(w in sent for w in BYLINE_WORDS):
            continue
        if any(w in sent for w in DOMESTIC_WORDS) and not any(w in sent for w in INTL_WORDS):
            continue
        if (PRICE_RE.search(sent) and any(w in sent for w in ("元", "折", "NT", "TWD"))
                and not any(w in sent for w in ("股", "營收", "獲利", "EPS", "市值"))):
            sent = sent.strip()
            m = PRICE_RE.search(sent)
            start = max(0, m.start() - 35)
            return ("…" if start else "") + sent[start:start + 70] + ("…" if len(sent) > start + 70 else "")
    return None


def read_body(item):
    """點進新聞讀內文，存在 item['body']；讀不到回傳空字串"""
    if "body" in item:
        return item["body"]
    item["body"] = ""
    try:
        url = decode_google_news(item["link"]) or item["link"]
        if "news.google.com" not in url:
            item["body"] = article_text(url)
            item["link"] = url
    except Exception as e:
        log(f"  讀內文失敗：{item['title'][:20]}…（{type(e).__name__}）")
    return item["body"]


def is_domestic_body(item):
    """標題沒有國外地名，內文提到國內線／離島，而且內文也沒有國外地名 → 視為國內線"""
    if any(w in item["title"] for w in INTL_WORDS):
        return False
    body = item.get("body") or ""
    dom = any(w in body for w in DOMESTIC_WORDS) or any(w in body for w in PACKAGE_WORDS)
    return dom and not any(w in body for w in INTL_WORDS)


def check_detail(item):
    """點進去看內文，有價格就把那句話放進 item['detail']；回傳 True/False"""
    sent = price_sentence(read_body(item))
    if sent:
        item["detail"] = sent
        return True
    return False


# ── 特價開賣後加查（叫降價追蹤去查）──
AIRLINE_CODE = {"台灣虎航": "IT", "虎航": "IT", "樂桃": "MM", "酷航": "TR", "捷星": "GK", "亞航": "AK", "亞洲航空": "AK",
                "越捷": "VJ", "濟州航": "7C", "真航空": "LJ", "易斯達": "ZE", "德威": "TW", "釜山航空": "BX",
                "香港快運": "UO", "香港航空": "HX", "國泰": "CX", "泰國獅": "SL", "獅航": "SL", "宿霧太平洋": "5J",
                "星宇": "JX", "長榮": "BR", "華航": "CI", "中華航空": "CI", "華信": "AE", "立榮": "B7",
                "大韓航空": "KE", "韓亞": "OZ", "日本航空": "JL", "日航": "JL", "全日空": "NH", "越南航空": "VN",
                "越航": "VN", "泰國航空": "TG", "泰航": "TG", "新加坡航空": "SQ", "新航": "SQ", "菲律賓航空": "PR"}
AIRLINE_FULL = {"IT": "台灣虎航", "MM": "樂桃航空", "TR": "酷航", "GK": "捷星日本航空", "AK": "亞洲航空", "VJ": "越捷航空",
                "7C": "濟州航空", "LJ": "真航空", "ZE": "易斯達航空", "TW": "德威航空", "BX": "釜山航空", "UO": "香港快運航空",
                "HX": "香港航空", "CX": "國泰航空", "SL": "泰國獅子航空", "5J": "宿霧太平洋航空", "JX": "星宇航空",
                "BR": "長榮航空", "CI": "中華航空", "AE": "華信航空", "B7": "立榮航空", "KE": "大韓航空", "OZ": "韓亞航空",
                "JL": "日本航空", "NH": "全日空", "VN": "越南航空", "TG": "泰國國際航空", "SQ": "新加坡航空", "PR": "菲律賓航空"}
CITY_CODE = {"台北": "TPE", "臺北": "TPE", "桃園": "TPE", "松山": "TSA", "台中": "RMQ", "臺中": "RMQ", "高雄": "KHH",
             "台南": "TNN", "臺南": "TNN",
             "東京": "NRT", "成田": "NRT", "羽田": "HND", "大阪": "KIX", "關西": "KIX", "名古屋": "NGO", "福岡": "FUK",
             "沖繩": "OKA", "那霸": "OKA", "札幌": "CTS", "北海道": "CTS", "函館": "HKD", "旭川": "AKJ", "仙台": "SDJ",
             "新潟": "KIJ", "茨城": "IBR", "富山": "TOY", "小松": "KMQ", "靜岡": "FSZ", "岡山": "OKJ", "廣島": "HIJ",
             "高松": "TAK", "熊本": "KMJ", "鹿兒島": "KOJ", "宮崎": "KMI", "大分": "OIT", "長崎": "NGS", "佐賀": "HSG",
             "米子": "YGJ", "石垣島": "ISG", "石垣": "ISG", "宮古島": "SHI", "青森": "AOJ", "秋田": "AXT", "花卷": "HNA",
             "首爾": "ICN", "仁川": "ICN", "釜山": "PUS", "濟州": "CJU", "大邱": "TAE", "清州": "CJJ",
             "曼谷": "BKK", "清邁": "CNX", "普吉": "HKT", "峴港": "DAD", "河內": "HAN", "胡志明": "SGN", "芽莊": "CXR",
             "富國島": "PQC", "宿霧": "CEB", "馬尼拉": "MNL", "克拉克": "CRK", "新加坡": "SIN", "吉隆坡": "KUL",
             "檳城": "PEN", "澳門": "MFM", "香港": "HKG", "關島": "GUM", "帛琉": "ROR", "峇里島": "DPS"}
ROUTE_RE = re.compile(r"(" + "|".join(sorted(CITY_CODE, key=len, reverse=True)) + r")\s*[-－–—到飛至⇄↔]\s*(" +
                      "|".join(sorted(CITY_CODE, key=len, reverse=True)) + r")\s*(?:單程|來回)?\s*(\d[\d,]*)?\s*元?")


def sale_info(item):
    """從標題和內文找出：哪家航空、哪些航線（和新聞價格）、優惠的出發期間"""
    text = item["title"] + "\n" + (item.get("body") or "")
    code = ""
    for k in sorted(AIRLINE_CODE, key=len, reverse=True):
        if k in item["title"]:
            code = AIRLINE_CODE[k]
            break
    routes, have = [], set()
    for m in ROUTE_RE.finditer(text):
        a, b = CITY_CODE[m.group(1)], CITY_CODE[m.group(2)]
        if a == b or (a, b) in have or a not in ("TPE", "TSA", "RMQ", "KHH", "TNN"):
            continue
        have.add((a, b))
        routes.append({"from": a, "to": b, "fromName": m.group(1), "toName": m.group(2),
                       "price": int(m.group(3).replace(",", "")) if m.group(3) else None})
    travel = None
    pub = item["pub"] or datetime.now(TW)
    m = re.search(r"(?:出發|旅遊|搭乘)(?:區間|期間|日期)?自?\s*(明年)?(\d{1,2})月(\d{1,2})日\s*(?:\S{0,4})?\s*至\s*(明年)?(\d{1,2})月(\d{1,2})日", text)
    if m:
        def mk(nexty, mo, dd):
            y = pub.year + (1 if nexty or int(mo) < pub.month - 1 else 0)
            try:
                return date(y, int(mo), int(dd)).isoformat()
            except ValueError:
                return None
        travel = [mk(m.group(1), m.group(2), m.group(3)), mk(m.group(4) or m.group(1), m.group(5), m.group(6))]
    return {"airline": AIRLINE_FULL.get(code, ""), "code": code, "routes": routes[:4], "travel": travel or [None, None]}


def dispatch_sale_check(chk, rnd, after=None):
    """叫 GitHub 執行「降價追蹤」的特價加查"""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        log("（沒有 GITHUB_TOKEN，略過特價加查）")
        return False
    payload = {k: chk[k] for k in ("airline", "code", "routes", "travel", "title")}
    payload["round"] = rnd
    payload["after"] = after
    body = json.dumps({"ref": "main", "inputs": {"sale": json.dumps(payload, ensure_ascii=False)}}).encode()
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/track.yml/dispatches", data=body,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        log(f"已叫降價追蹤加查（第 {rnd} 次）：{chk['title'][:20]}…")
        return True
    except Exception as e:
        log(f"特價加查失敗：{e}")
        return False


SALE_AFTER = (15, 75)   # 開賣後幾分鐘加查（一般航空，用 Google Flights）
TIGER_AFTER = (1, 15, 30, 60)  # 虎航：直接看官網價格，開賣後 1、15、30、60 分鐘


def sale_rounds(r):
    """這筆特價要在開賣後第幾分鐘加查：[(第幾次, 幾分鐘), ...]"""
    mins = TIGER_AFTER if r.get("code") == "IT" else SALE_AFTER
    return list(enumerate(mins, 1))
WAIT_WINDOW = 80        # 接下來幾分鐘內有事情要做，就留著等，時間到準時做


def upcoming_events(reminders):
    """接下來要準時做的事（提醒、加查）的時間"""
    now = datetime.now(TW)
    out = []
    for r in reminders:
        if r.get("kind") == "tigerbase":
            continue
        start = datetime.fromisoformat(r["start"])
        if r.get("kind") == "sale":
            for rnd, after in sale_rounds(r):
                if rnd not in r.get("done", []):
                    out.append(start + timedelta(minutes=after))
        elif not r.get("sent"):
            out.append(start - timedelta(minutes=REMIND_BEFORE))
    return sorted(t for t in out if now < t <= now + timedelta(minutes=WAIT_WINDOW))


def wait_and_fire(reminders):
    """等到時間整點準時提醒／加查，最多等 WAIT_WINDOW 分鐘"""
    deadline = datetime.now(TW) + timedelta(minutes=WAIT_WINDOW + 5)
    while datetime.now(TW) < deadline:
        ev = upcoming_events(reminders)
        if not ev or ev[0] > deadline:
            return reminders  # 太久以後的，交給下一次執行
        wait = (ev[0] - datetime.now(TW)).total_seconds()
        log(f"⏳ 等 {int(wait // 60)} 分 {int(wait % 60)} 秒，到 {ev[0]:%H:%M} 準時提醒或加查…")
        time.sleep(max(1, wait) + 2)
        reminders = check_reminders(reminders)
        run_sale_checks(reminders)
        save_json(REMIND_FILE, reminders)
    return reminders


def run_sale_checks(reminders):
    """開賣後 20 分鐘、80 分鐘各加查一次"""
    now = datetime.now(TW)
    for r in reminders:
        if r.get("kind") != "sale":
            continue
        start = datetime.fromisoformat(r["start"])
        done = r.setdefault("done", [])
        due = [(rnd, after) for rnd, after in sale_rounds(r)
               if rnd not in done and now >= start + timedelta(minutes=after) and now < start + timedelta(hours=4)]
        if due:
            rnd, after = due[-1]  # 晚到時只做最新的那一次，前面錯過的不補
            if dispatch_sale_check(r, rnd, after):
                done.extend(x for x, _ in due)


# ── 直接盯虎航官網：不用等新聞，官網突然大降價就通知 ──
TIGER_DAYS = 270          # 看未來幾天的票價
TIGER_DROP_MANY = 0.85    # 3 條以上航線最低價變成原本的 85% 以下
TIGER_DROP_ONE = 0.70     # 或任何一條變成原本的 70% 以下
TIGER_COOLDOWN_H = 12     # 同一條航線通知過後，幾小時內不再通知


def tiger_watch(reminders):
    """每小時看一次虎航官網所有航線的最低價，跟上次比；大降價就推播，並觸發降價追蹤的虎航檢查"""
    try:
        import tiger
    except Exception:
        return reminders
    base = next((r for r in reminders if r.get("kind") == "tigerbase"), None)
    if base is None:
        base = {"kind": "tigerbase", "routes": {}, "alerted": {}}
        reminders.append(base)
    now = datetime.now(TW)
    lo, hi = (date.today() + timedelta(days=1)).isoformat(), (date.today() + timedelta(days=TIGER_DAYS)).isoformat()
    cur = {}
    for st in tiger.TW_STATIONS:
        rows = tiger._fetch(st)
        if not rows:
            continue
        for o, d in {(r.get("origin"), r.get("destination")) for r in rows}:
            if st not in (o, d):
                continue
            days = {k: v for k, v in tiger.daily(o, d).items() if lo <= k <= hi}
            if days:
                low = min(days.values())
                cur[f"{o}-{d}"] = {"min": low, "days": sorted(k for k, v in days.items() if v == low)[:5]}
    if not cur:
        log("虎航官網這次沒抓到資料")
        return reminders
    old, alerted = base.get("routes") or {}, base.setdefault("alerted", {})
    drops = []
    for k, v in cur.items():
        o = (old.get(k) or {}).get("min")
        if not o or v["min"] >= o:
            continue
        last = alerted.get(k)
        if last and now - datetime.fromisoformat(last) < timedelta(hours=TIGER_COOLDOWN_H):
            continue
        drops.append((v["min"] / o, k, o, v))
    big = [x for x in drops if x[0] <= TIGER_DROP_ONE]
    many = [x for x in drops if x[0] <= TIGER_DROP_MANY]
    hit = sorted(many if len(many) >= 3 else big, key=lambda x: x[0])
    base["routes"] = cur
    base["at"] = now.isoformat()
    if not old:
        log(f"虎航官網：第一次記錄 {len(cur)} 條航線的最低價")
        return reminders
    if not hit:
        log(f"虎航官網：{len(cur)} 條航線，沒有大降價")
        return reminders
    lines = [f"🐯 <b>虎航官網出現特價！</b>\n{len(hit)} 條航線大降價（直接看官網發現的，不是新聞）\n"]
    for ratio, k, o, v in hit[:10]:
        a, b = k.split("-")
        ds = "、".join(f"{int(x[5:7])}/{int(x[8:])}" for x in v["days"])
        lines.append(f"・{tiger.name(a)} → {tiger.name(b)}：NT${o:,} → <b>NT${v['min']:,}</b>（降 {round((1 - ratio) * 100)}%）　{ds}")
        alerted[k] = now.isoformat()
    lines.append("\n<i>單程未稅起價，結帳會再加稅金。特價座位很少，建議盡快到官網看。</i>"
                 "\n🔗 <a href=\"https://www.tigerairtw.com/\">到虎航官網訂票</a>")
    send_telegram("\n".join(lines))
    # 順便觸發降價追蹤的虎航檢查（你的追蹤＋這些航線）
    routes = []
    for _, k, _, v in hit[:6]:
        a, b = k.split("-")
        if a in tiger.TW_STATIONS:
            routes.append({"from": a, "to": b, "fromName": tiger.name(a), "toName": tiger.name(b), "price": v["min"]})
    reminders.append({"kind": "sale", "start": (now - timedelta(minutes=1)).isoformat(),
                      "title": "虎航官網偵測到特價", "airline": "台灣虎航", "code": "IT",
                      "routes": routes, "travel": [None, None]})
    return reminders


SALE_KEYS = ["開賣", "開搶", "銷售", "搶購", "開放購票", "開放訂位", "限時", "快閃", "上架"]
BYLINE_WORDS = ["記者", "特派", "｜", "|", "©", "http", "報導", "編輯", "撰文", "{", "}"]


def body_sale_times(text, pub, title=""):
    """從內文找出開賣時間：有「開賣、銷售期間…」的句子裡的日期時間。回傳 [(時間, 是否推測, 說明)]"""
    out, have = [], set()
    head = re.sub(r"\s+", "", title)[:10]
    for sent in re.split(r"[。！\n]", text or ""):
        flat = re.sub(r"\s+", "", sent)
        if len(flat) > 220 or any(w in sent for w in BYLINE_WORDS) or (head and head in flat):
            continue  # 署名、發稿時間、跟標題重複的那行，不拿來找開賣時間
        pos = [sent.find(k) for k in SALE_KEYS if k in sent]
        if not pos:
            continue
        # 只看「開賣、銷售」前後那一段；「出發、旅遊、回程」之後的日期是搭機日期，不是開賣日期
        cut = len(sent)
        for w in ("出發", "旅遊", "回程", "適用", "搭乘", "飛行期間", "航班日期"):
            i = sent.find(w, min(pos))
            if i != -1:
                cut = min(cut, i)
        sent = sent[:cut]
        st = parse_start(sent, pub)
        if not st or st[0] in have:
            continue
        have.add(st[0])
        ctx = re.sub(r"\s+", "", sent)
        ctx = ctx[:46] + ("…" if len(ctx) > 46 else "")
        out.append((st[0], st[1], ctx))
    return out[:3]


def is_promo(item):
    if item["pub"] and item["pub"] < datetime.now(TW) - timedelta(days=MAX_AGE_DAYS):
        return False
    return deal_level(item["title"]) == "yes"


def promo_level(item):
    if item["pub"] and item["pub"] < datetime.now(TW) - timedelta(days=MAX_AGE_DAYS):
        return "no"
    return deal_level(item["title"])


WEEK = "一二三四五六日"


def parse_start(title, pub):
    """從標題找出開搶時間。回傳 (datetime, 是否用猜的)；找不到回傳 None"""
    t = title.replace("：", ":").replace("（", "(").replace("）", ")")
    m = re.search(r"(上午|早上|中午|下午|晚上|晚間|凌晨|早)?\s*(?<!\d)(\d{1,2}):(\d{2})", t)
    if m:
        ap, h, mi = m.group(1), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(上午|早上|中午|下午|晚上|晚間|凌晨|早)?\s*(?<![\d.,])(\d{1,2})\s*[點時](半)?", t)
        if not m:
            return None
        ap, h, mi = m.group(1), int(m.group(2)), 30 if m.group(3) else 0
    if ap in ("下午", "晚上", "晚間") and h < 12:
        h += 12
    if h > 23 or mi > 59:
        return None
    base = (pub or datetime.now(TW)).astimezone(TW)
    guessed = False
    md = re.search(r"(\d{1,2})月(\d{1,2})[日號]", t)
    if md:
        mo, dd = int(md.group(1)), int(md.group(2))
        yr = base.year + (1 if mo < base.month - 6 else 0)
        try:
            d = date(yr, mo, dd)
        except ValueError:
            return None
    elif re.search(r"明[早天日晚(]|明\d|後天|今[早天日晚(]|今\d", t):
        # 有好幾個（例如「今天上午10時至明天晚上」）時，以最先出現的為準
        m2 = re.search(r"明[早天日晚(]|明\d|後天|今[早天日晚(]|今\d", t).group(0)
        d = base.date() + timedelta(days=2 if m2 == "後天" else 1 if m2.startswith("明") else 0)
    else:
        guessed = True
        d = base.date()
        if datetime(d.year, d.month, d.day, h, mi, tzinfo=TW) <= base:
            d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, h, mi, tzinfo=TW), guessed


def calendar_link(item, start):
    end = start + timedelta(hours=1)
    f = lambda x: x.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    q = urllib.parse.urlencode({"action": "TEMPLATE", "text": "✈️ 機票開搶：" + item["title"][:60],
                                "dates": f"{f(start)}/{f(end)}", "details": item["link"]})
    return "https://calendar.google.com/calendar/render?" + q


def fmt_time(dt):
    return f"{dt.month}/{dt.day}（{WEEK[dt.weekday()]}）{dt:%H:%M}"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def check_reminders(reminders):
    """開搶前 30 分鐘左右提醒；已經過期的清掉"""
    now = datetime.now(TW)
    keep = []
    for r in reminders:
        if r.get("kind") == "tigerbase":
            keep.append(r)
            continue
        start = datetime.fromisoformat(r["start"])
        if r.get("kind") == "sale":
            if now <= start + timedelta(hours=4):
                keep.append(r)
            continue
        if now > start + timedelta(hours=1):
            continue  # 過期了
        base_title = r["title"].split("（")[0]
        if (any(w in base_title for w in EXCLUDE_WORDS + PROMO_EXCLUDE + PACKAGE_WORDS)
                or "{" in r["title"] or "@context" in r["title"] or "http" in r["title"]
                or deal_level(base_title) == "no"):
            continue  # 舊版留下的錯誤提醒（股票、抽獎、亂碼…），不傳、直接丟掉
        if not r.get("sent") and now >= start - timedelta(minutes=REMIND_BEFORE + 1):
            left = round((start - now).total_seconds() / 60)
            head = f"⏰ <b>{left} 分鐘後開搶！</b>" if left > 0 else "⏰ <b>已經開搶了，快去搶！</b>"
            send_telegram(f"{head}\n✈️ {html.escape(r['title'])}\n開搶時間：{fmt_time(start)}"
                          f"\n<a href=\"{html.escape(r['link'])}\">看新聞</a>")
            r["sent"] = True
            log(f"已提醒：{r['title']}")
        keep.append(r)
    return keep


def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("（沒有設定 TELEGRAM_TOKEN / TELEGRAM_CHAT_ID，只印在畫面上）\n" + text)
        return
    body = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode("utf-8")
    fetch(f"https://api.telegram.org/bot{token}/sendMessage", data=body)


def fmt(item):
    when = item["pub"].strftime("%m/%d %H:%M") if item["pub"] else ""
    meta = "　".join(x for x in (item["source"], when) if x)
    out = (f"✈️ <b>{html.escape(item['title'])}</b>\n"
           f"<i>{html.escape(meta)}</i>\n"
           + (f"💰 {html.escape(item['detail'])}\n" if item.get("detail") else ""))
    links = []
    for start, guessed, ctx in item.get("starts") or []:
        note = f"\n　　<i>{html.escape(ctx)}</i>" if ctx else ""
        if start > datetime.now(TW):
            out += (f"⏰ 開搶：<b>{fmt_time(start)}</b>" + ("（推測）" if guessed else "") +
                    f"　會在 {REMIND_BEFORE} 分鐘前提醒你{note}\n")
            links.append(f"<a href=\"{html.escape(calendar_link(item, start))}\">📅 {start.month}/{start.day} 加到行事曆</a>")
        else:
            out += f"🔥 已在 {fmt_time(start)} 開賣{note}\n"
    return out + "　".join(links + [f"<a href=\"{html.escape(item['link'])}\">看新聞</a>"])


def send_batches(header, items):
    """Telegram 一則訊息最多 4096 字，太多就分批"""
    chunk = header
    for it in items:
        part = "\n\n" + fmt(it)
        if len(chunk) + len(part) > 3800:
            send_telegram(chunk)
            time.sleep(1)
            chunk = header + "（續）"
        chunk += part
    send_telegram(chunk)


def main():
    if "--test" in sys.argv:
        send_telegram("✅ 特價快報測試成功！之後發現廉航促銷消息，就會傳到這裡。")
        log("已傳送測試訊息")
        return

    reminders = check_reminders(load_json(REMIND_FILE, []))
    if "--test" not in sys.argv and (datetime.now(TW).minute < 30 or "--search" in sys.argv):
        reminders = tiger_watch(reminders)   # 每小時一次（整點後那一次）
    run_sale_checks(reminders)

    first_run = not os.path.exists(SEEN_FILE)
    now = datetime.now(TW)
    due = first_run or "--search" in sys.argv or (now.hour % SEARCH_EVERY_HOURS == 0 and now.minute < 30)
    if not due:
        log("這次只檢查提醒，不搜尋新聞。")
        save_json(REMIND_FILE, reminders)
        if "--test" not in sys.argv:
            save_json(REMIND_FILE, wait_and_fire(reminders))
        return
    seen = [] if first_run else load_json(SEEN_FILE, [])
    seen_set = set(seen)

    keywords = load_keywords()
    found, maybe, failed = {}, {}, 0
    for kw in keywords:
        try:
            for it in search_news(kw):
                if it["id"] in seen_set:
                    continue
                lv = promo_level(it)
                # 不同關鍵字搜到同一則，只算一次（用標題判斷）
                if lv == "yes":
                    found.setdefault(it["title"], it)
                elif lv == "maybe":
                    maybe.setdefault(it["title"], it)
            log(f"✓ {kw}")
        except Exception as e:
            failed += 1
            log(f"✗ {kw}：{e}")
        time.sleep(1.5)

    # 標題沒寫價格的，點進去看內文（最新的優先，最多 MAX_DETAIL 篇）
    cands = sorted((v for k, v in maybe.items() if k not in found),
                   key=lambda x: x["pub"] or datetime.min.replace(tzinfo=TW), reverse=True)[:MAX_DETAIL]
    for it in cands:
        if check_detail(it):
            found[it["title"]] = it
            log(f"  ✓ 內文有價格：{it['title'][:24]}…")
        else:
            seen.append(it["id"])  # 內文也沒價格：記起來，下次不用再點
        time.sleep(1)
    # 標題就符合的，也點進去看內文，找內文裡的開賣時間（例如標題寫這波、內文預告下一波）
    budget = MAX_DETAIL
    for it in sorted(found.values(), key=lambda x: x["pub"] or datetime.min.replace(tzinfo=TW), reverse=True):
        if "body" in it:
            continue
        if budget <= 0:
            break
        read_body(it)
        budget -= 1
        time.sleep(1)
    # 標題沒寫國外地名、內文卻是國內線（離島、本島）或套裝行程的，不推
    for k in list(found):
        it = found[k]
        if is_domestic_body(it):
            log(f"  ✗ 內文是國內線或套裝行程，不推：{it['title'][:24]}…")
            seen.append(it["id"])
            del found[k]

    new = sorted(found.values(), key=lambda x: x["pub"] or datetime.min.replace(tzinfo=TW), reverse=True)
    log(f"新的促銷消息：{len(new)} 則")

    # 找開搶時間，加入提醒清單（同一個開搶時間＋相同航空只提醒一次）
    have = {(r["start"], r["title"][:6]) for r in reminders if r.get("kind") != "tigerbase"}
    have |= {("sale", r["start"], r.get("code")) for r in reminders if r.get("kind") == "sale"}
    for it in new:
        starts = []
        st = parse_start(it["title"], it["pub"])
        if st:
            starts.append((st[0], st[1], ""))
        for t2, g2, ctx in body_sale_times(it.get("body"), it["pub"], it["title"]):
            if all(abs((t2 - x[0]).total_seconds()) > 60 for x in starts):
                starts.append((t2, g2, ctx))
        it["starts"] = starts
        info = None
        for t2, g2, ctx in starts:
            if t2 > now:
                info = info or sale_info(it)
                skey = ("sale", t2.isoformat(), info["code"])
                if info["code"] and skey not in have:
                    reminders.append({"kind": "sale", "start": t2.isoformat(), "title": it["title"], **info})
                    have.add(skey)
                key = (t2.isoformat(), it["title"][:6])
                if key not in have:
                    reminders.append({"title": it["title"] + (f"（{ctx}）" if ctx else ""), "link": it["link"],
                                      "start": t2.isoformat(), "sent": False})
                    have.add(key)

    if failed == len(keywords) and keywords:
        send_telegram("⚠️ 特價快報這次查詢全部失敗，可能是暫時連不上新聞來源。下次會自動再試。")
    elif first_run:
        head = (f"✅ <b>特價快報開始運作！</b>\n之後有新的機票促銷就會通知你。\n"
                f"以下是最近 {MAX_DAYS_TXT} 符合條件的 {min(len(new), 15)} 則：")
        if new:
            send_batches(head, new[:15])
        else:
            send_telegram(head.replace("以下是最近的幾則：", "目前最近幾天沒有促銷新聞。"))
    elif new:
        send_batches(f"🔥 <b>發現 {len(new)} 則新的機票促銷消息</b>", new)

    for it in new:
        seen.append(it["id"])
    # 第一次執行：把目前搜到的全部記起來，避免下次一次通知一大堆
    if first_run:
        for kw in keywords:
            try:
                seen.extend(i["id"] for i in search_news(kw))
            except Exception:
                pass
    seen = list(dict.fromkeys(seen))[-KEEP_SEEN:]
    save_json(SEEN_FILE, seen)
    save_json(REMIND_FILE, reminders)
    save_json(REMIND_FILE, wait_and_fire(reminders))


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
