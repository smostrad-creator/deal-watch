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
TIME_RE = re.compile(r"\d{1,2}\s*[點時]|\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|\d{1,2}月\d{1,2}[日號]|[今明]\s*\(?\d{0,2}\)?\s*[日天早晚]")
SALE_WORDS = ["開賣", "開搶", "搶票", "快閃", "限時", "秒殺"]


def is_ticket_deal(t):
    return deal_level(t) == "yes"


def deal_level(t):
    """yes＝標題就符合；maybe＝有機票和開賣時間、但標題沒寫價格（要點進去看內文）；no＝不符合"""
    flight = any(w in t for w in FLIGHT_WORDS)
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
    parts = []
    for pat in (r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"',
                r'<meta[^>]+name="description"[^>]+content="([^"]+)"'):
        m = re.search(pat, page)
        if m:
            parts.append(html.unescape(m.group(1)))
    for p in re.findall(r"<p[^>]*>(.*?)</p>", page, flags=re.S)[:12]:
        t = html.unescape(re.sub(r"<[^>]+>", "", p)).strip()
        if len(t) > 15:
            parts.append(t)
    return "\n".join(parts)


def price_sentence(text):
    """找出有價格的那一句（最多 70 個字）"""
    for sent in re.split(r"[。！\n]", text):
        if PRICE_RE.search(sent) and any(w in sent for w in ("元", "折", "NT", "TWD")):
            sent = sent.strip()
            m = PRICE_RE.search(sent)
            start = max(0, m.start() - 35)
            return ("…" if start else "") + sent[start:start + 70] + ("…" if len(sent) > start + 70 else "")
    return None


def check_detail(item):
    """點進去看內文，有價格就把那句話放進 item['detail']；回傳 True/False"""
    try:
        url = decode_google_news(item["link"]) or item["link"]
        if "news.google.com" in url:
            return False
        sent = price_sentence(article_text(url))
        if sent:
            item["detail"] = sent
            item["link"] = url
            return True
    except Exception as e:
        log(f"  讀內文失敗：{item['title'][:20]}…（{type(e).__name__}）")
    return False


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
    m = re.search(r"(上午|早上|中午|下午|晚上|晚間|凌晨|早)?\s*(\d{1,2}):(\d{2})", t)
    if m:
        ap, h, mi = m.group(1), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(上午|早上|中午|下午|晚上|晚間|凌晨|早)?\s*(\d{1,2})\s*[點時](半)?", t)
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
    elif re.search(r"明[早天日晚(]|明\d", t):
        d = base.date() + timedelta(days=1)
    elif "後天" in t:
        d = base.date() + timedelta(days=2)
    elif re.search(r"今[早天日晚(]|今\d", t):
        d = base.date()
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
        start = datetime.fromisoformat(r["start"])
        if now > start + timedelta(hours=1):
            continue  # 過期了
        if not r.get("sent") and now >= start - timedelta(minutes=REMIND_BEFORE + 15):
            left = int((start - now).total_seconds() // 60)
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
    st = item.get("start")
    if st:
        start, guessed = st
        if start > datetime.now(TW):
            out += f"⏰ 開搶：<b>{fmt_time(start)}</b>" + ("（從標題推測）" if guessed else "") + \
                   f"　會在 {REMIND_BEFORE} 分鐘前提醒你\n" + \
                   f"<a href=\"{html.escape(calendar_link(item, start))}\">📅 加到 Google 行事曆</a>　"
        else:
            out += f"🔥 已在 {fmt_time(start)} 開賣\n"
    return out + f"<a href=\"{html.escape(item['link'])}\">看新聞</a>"


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

    first_run = not os.path.exists(SEEN_FILE)
    now = datetime.now(TW)
    due = first_run or "--search" in sys.argv or (now.hour % SEARCH_EVERY_HOURS == 0 and now.minute < 30)
    if not due:
        log("這次只檢查提醒，不搜尋新聞。")
        save_json(REMIND_FILE, reminders)
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

    new = sorted(found.values(), key=lambda x: x["pub"] or datetime.min.replace(tzinfo=TW), reverse=True)
    log(f"新的促銷消息：{len(new)} 則")

    # 找開搶時間，加入提醒清單（同一個開搶時間＋相同航空只提醒一次）
    have = {(r["start"], r["title"][:6]) for r in reminders}
    for it in new:
        st = parse_start(it["title"], it["pub"])
        it["start"] = st
        if st and st[0] > now:
            key = (st[0].isoformat(), it["title"][:6])
            if key not in have:
                reminders.append({"title": it["title"], "link": it["link"], "start": st[0].isoformat(), "sent": False})
                have.add(key)

    if failed == len(keywords) and keywords:
        send_telegram("⚠️ 特價快報這次查詢全部失敗，可能是暫時連不上新聞來源。下次會自動再試。")
    elif first_run:
        head = "✅ <b>特價快報開始運作！</b>\n之後有新的廉航促銷消息就會通知你。\n以下是最近的幾則："
        if new:
            send_batches(head, new[:5])
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


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
