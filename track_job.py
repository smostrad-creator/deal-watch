# -*- coding: utf-8 -*-
"""
降價追蹤：由 GitHub Actions 每 2 小時執行。
讀取 tracks.json（查詢頁新增的追蹤清單），查每一條的目前最低價，
跟上次比較，符合通知條件就傳 Telegram。狀態存在 track_state.json。
"""
import html
import json
import os
import sys
import traceback
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TRACKS = os.path.join(HERE, "tracks.json")
STATE = os.path.join(HERE, "track_state.json")
TW = timezone(timedelta(hours=8))
MAX_SCAN_DAYS = 45      # 區間追蹤最多幾個出發日（太多會很慢，也容易被 Google 擋）
WEEK = "一二三四五六日"

sys.path.insert(0, HERE)
import 查機票 as core  # noqa: E402
import tiger  # noqa: E402  虎航官網每日最低價

core.print = lambda *a, **k: print(*a, flush=True)
from search_job import send_text, fmtp  # noqa: E402  共用 Telegram 傳送


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def dlabel(d):
    return f"{d.month}/{d.day}({WEEK[d.weekday()]})"


def route_of(t):
    return f"{t.get('originLabel') or t['origin']} ⇄ {t.get('destLabel') or t['dest']}"


def pairs_of(t):
    """這條追蹤現在還要查的日期組合（已經過去的不查）"""
    tomorrow = date.today() + timedelta(days=1)
    if t["kind"] == "scan":
        s = t["scan"]
        days = int(s["days"])
        if s.get("startDate"):
            first, last = date.fromisoformat(s["startDate"]), date.fromisoformat(s["endDate"])
        else:
            y1, m1 = map(int, s["startMonth"].split("-"))
            y2, m2 = map(int, s["endMonth"].split("-"))
            first = date(y1, m1, 1)
            last = date(y2 + (m2 == 12), m2 % 12 + 1, 1) - timedelta(days=1)
        first = max(first, tomorrow)
        out, d = [], first
        while d + timedelta(days=days - 1) <= last and len(out) < MAX_SCAN_DAYS:
            out.append((d, d + timedelta(days=days - 1)))
            d += timedelta(days=1)
        return out
    return [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in t["pairs"]
            if date.fromisoformat(a) >= tomorrow]


def dates_desc(t):
    if t["kind"] == "scan":
        s = t["scan"]
        if s.get("startDate"):
            a, b = date.fromisoformat(s["startDate"]), date.fromisoformat(s["endDate"])
            rng = f"{dlabel(a)}～{dlabel(b)}"
        else:
            ym = lambda v: f"{v[:4]}年{int(v[5:7])}月"
            rng = ym(s["startMonth"]) if s["startMonth"] == s["endMonth"] else f"{ym(s['startMonth'])}～{ym(s['endMonth'])}"
        return f"{rng}，每天出發、玩 {s['days']} 天"
    ps = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in t["pairs"]]
    if len(ps) == 1:
        return f"{dlabel(ps[0][0])}～{dlabel(ps[0][1])}"
    return f"{len(ps)} 組日期（{dlabel(ps[0][0])} 起）"


def notify_desc(n):
    m = n.get("mode")
    if m == "below":
        return f"低於 NT${int(n.get('target') or 0):,} 時通知"
    if m == "dropBy":
        return f"降價超過 NT${int(n.get('amount') or 0):,} 時通知"
    if m == "any":
        return "價格有變動就通知（漲跌都通知）"
    return "有降價就通知"


def setup_core(t):
    core.出發地, core.目的地 = t["origin"], t["dest"]
    st = t.get("stops")
    core.轉機 = None if st in (None, "", "any") else int(st)
    core.大人人數 = int(t.get("adults") or 1)
    core.去程最早出發, core.去程最晚出發 = t.get("outFrom"), t.get("outTo")
    core.回程最早出發, core.回程最晚出發 = t.get("retFrom"), t.get("retTo")
    core.只看這些航空公司 = []
    core.補抓完整清單 = False
    core.補查沒價格的航班 = False


def tiger_ok_for(t):
    """這條追蹤要不要查虎航官網：全部航空、或有勾台灣虎航，而且是台灣出發"""
    return ((not t.get("airlines")) or "台灣虎航" in t["airlines"]) and t["origin"] in tiger.TW_STATIONS


def tiger_line(tb):
    total, d1, d2, a, b = tb
    return (f"🐯 虎航官網起價：<b>{fmtp(total)}</b>（未稅來回）"
            f"\n　　去 {dlabel(d1)} {fmtp(a)}、回 {dlabel(d2)} {fmtp(b)}")


def check_tiger(t, s, pairs, route, notify=True):
    """查虎航官網的未稅來回起價，降價就通知；回傳要附在訊息裡的文字（或空字串）"""
    if not tiger_ok_for(t):
        return ""
    tb = tiger.round_trip(t["origin"], t["dest"], pairs)
    if tb is None:
        return ""
    ts = s.setdefault("tiger", {})
    last = ts.get("last")
    total = tb[0]
    if notify and last is not None and total < last:
        send_text(f"🐯 <b>虎航官網降價了！</b>\n{route}\n{fmtp(last)} → <b>{fmtp(total)}</b>（降 NT${last - total:,}，未稅來回）"
                  f"\n去 {dlabel(tb[1])} {fmtp(tb[3])}、回 {dlabel(tb[2])} {fmtp(tb[4])}"
                  + (f"\n虎航官網最低紀錄：{fmtp(min(total, ts.get('min') or total))}") +
                  "\n🔗 <a href=\"https://www.tigerairtw.com/\">到虎航官網訂票</a>")
    ts.update(last=total, d1=tb[1].isoformat(), d2=tb[2].isoformat(), at=datetime.now(TW).strftime("%m/%d %H:%M"))
    if ts.get("min") is None or total < ts["min"]:
        ts["min"] = total
    return tiger_line(tb)


def cheapest(t, pairs):
    """查每組日期，回傳全部裡面最便宜的一筆；全部查詢都失敗回傳 None"""
    keep = set(t.get("airlines") or [])
    best, ok_any = None, False
    for d1, d2 in pairs:
        q = core.make_query(d1, d2)
        try:
            flights = core.get_flights_complete(q)
        except Exception as e:
            print("查詢失敗：", e)
            continue
        ok_any = True
        for f in flights:
            it = core.describe(f)
            if not it or not core.in_hours(it["hour"], core.去程最早出發, core.去程最晚出發):
                continue
            if keep and it["airline"] not in keep:
                continue
            if best is None or it["price"] < best["price"]:
                best = {"price": it["price"], "airline": it["airline"], "out": it["text"],
                        "d1": d1.isoformat(), "d2": d2.isoformat(),
                        "dates": f"{dlabel(d1)}～{dlabel(d2)}", "url": q.url()}
    return best if ok_any else None


SERP_PER_RUN = 5       # 降價追蹤每次最多用幾次 SerpApi
SERP_PER_MONTH = 100   # 降價追蹤每月最多用幾次（其餘留給手機查機票）


def serp_budget(state):
    """這次執行可以用幾次 SerpApi，並設定到 core"""
    month = datetime.now(TW).strftime("%Y-%m")
    b = state.setdefault("_serp", {"month": month, "used": 0})
    if b.get("month") != month:
        b.update(month=month, used=0)
    core.SERP_MAX = max(0, min(SERP_PER_RUN, SERP_PER_MONTH - b["used"]))
    core.SERP_USED = 0


def serp_done(state):
    state["_serp"]["used"] += getattr(core, "SERP_USED", 0)


def cheapest_oneway(frm, to, dates):
    """單程（只看直飛）在幾個日期裡的最低價；回傳 (價格, 日期, 時間) 或 None"""
    best = None
    for d in dates:
        try:
            flights = core.get_flights_complete(core.oneway_query(frm, to, d))
        except Exception as e:
            print("單程查詢失敗：", e)
            continue
        for f in flights:
            it = core.describe(f)
            if it and (best is None or it["price"] < best[0]):
                best = (it["price"], d, it["text"])
    return best


def tiger_sale_check(sale, tracks, state):
    """虎航特價：直接看虎航官網的每日最低價（未稅單程起價）"""
    after = sale.get("after")
    when = f"開賣後 {after} 分鐘" if after else f"第 {sale.get('round', 1)} 次"
    lines = [f"🐯 <b>虎航官網價格檢查</b>（{when}）", f"<i>{html.escape(sale.get('title', '')[:60])}</i>"]
    tomorrow = date.today() + timedelta(days=1)
    a, b = sale.get("travel") or [None, None]
    a = max(date.fromisoformat(a), tomorrow) if a else tomorrow
    b = date.fromisoformat(b) if b else a + timedelta(days=180)
    # A. 你的追蹤
    mine = [t for t in tracks if t.get("active", True) and not state.get(t["id"], {}).get("expired") and tiger_ok_for(t)]
    if mine:
        lines.append("\n<b>你的追蹤</b>（未稅來回起價）")
    for t in mine[:8]:
        s = state.setdefault(t["id"], {})
        before = (s.get("tiger") or {}).get("last")
        tb = tiger.round_trip(t["origin"], t["dest"], pairs_of(t))
        name = html.escape(route_of(t))
        if tb is None:
            lines.append(f"・{name}：" + ("虎航官網暫時抓不到資料" if not tiger.ok(t["origin"], t["dest"]) else "這些日期虎航沒有票價"))
            continue
        total = tb[0]
        diff = "" if before is None else (f"　📉 降 NT${before - total:,}" if total < before else "　沒有變便宜" if total == before else f"　漲 NT${total - before:,}")
        lines.append(f"・{name}：<b>{fmtp(total)}</b>（去 {dlabel(tb[1])} {fmtp(tb[3])}、回 {dlabel(tb[2])} {fmtp(tb[4])}）{diff}")
        ts = s.setdefault("tiger", {})
        ts.update(last=total, d1=tb[1].isoformat(), d2=tb[2].isoformat(), at=datetime.now(TW).strftime("%m/%d %H:%M"))
        if ts.get("min") is None or total < ts["min"]:
            ts["min"] = total
    # B. 新聞裡的特價航線
    routes = sale.get("routes") or []
    if routes:
        lines.append(f"\n<b>新聞裡的特價航線</b>（單程未稅起價，出發 {dlabel(a)}～{dlabel(b)}）")
    for r in routes[:6]:
        days = {d: p for d, p in tiger.daily(r["from"], r["to"]).items() if a.isoformat() <= d <= b.isoformat()}
        label = f"{html.escape(r.get('fromName') or tiger.name(r['from']))} → {html.escape(r.get('toName') or tiger.name(r['to']))}"
        news = f"　新聞 {r['price']:,} 元起" if r.get("price") else ""
        if not days:
            lines.append(f"・{label}：" + ("虎航官網暫時抓不到資料" if not tiger.ok(r["from"], r["to"]) else "這段期間沒有票價") + news)
            continue
        low = min(days.values())
        dates = sorted(d for d, p in days.items() if p == low)
        show = "、".join(dlabel(date.fromisoformat(d)) for d in dates[:5]) + (f" 等 {len(dates)} 天" if len(dates) > 5 else "")
        hit = "　✅ 有特價" if r.get("price") and low <= r["price"] else ""
        lines.append(f"・{label}：最低 <b>{fmtp(low)}</b>　{show}{news}{hit}")
    lines.append("\n<i>這是虎航官網的單人未稅起價，結帳會再加稅金；要輸入優惠碼的折扣這裡看不到。</i>"
                 "\n🔗 <a href=\"https://www.tigerairtw.com/\">到虎航官網訂票</a>")
    if mine or routes:
        send_text("\n".join(lines))


def sale_check(sale, tracks, state):
    """特價開賣後加查：A. 你的追蹤裡跟這家航空有關的；B. 新聞裡提到的特價航線"""
    airline, code = sale.get("airline", ""), sale.get("code", "")
    if code == "IT":
        return tiger_sale_check(sale, tracks, state)
    rnd = sale.get("round", 1)
    lines = [f"🔍 <b>{html.escape(airline)}特價開賣後檢查</b>（第 {rnd} 次）",
             f"<i>{html.escape(sale.get('title', '')[:60])}</i>"]
    # A. 你的追蹤
    mine = [t for t in tracks if t.get("active", True) and not state.get(t["id"], {}).get("expired")
            and (not t.get("airlines") or airline in t.get("airlines"))]
    if mine:
        lines.append("\n<b>你的追蹤</b>")
    for t in mine[:5]:
        pairs = pairs_of(t)[:8]  # 區間追蹤只抽前 8 天，避免太慢
        if not pairs:
            continue
        setup_core(t)
        best = cheapest(t, pairs)
        s = state.setdefault(t["id"], {})
        before = s.get("last")
        name = html.escape(route_of(t))
        if best is None:
            lines.append(f"・{name}：這次查詢失敗，晚點再試")
            continue
        p = best["price"]
        if before is None:
            lines.append(f"・{name}：目前 <b>{fmtp(p)}</b>（{html.escape(best['airline'])} {best['dates']}）")
        elif p < before:
            lines.append(f"・{name}：{fmtp(before)} → <b>{fmtp(p)}</b>（降 NT${before - p:,}）📉　{html.escape(best['airline'])} {best['dates']}")
        else:
            lines.append(f"・{name}：{fmtp(p)}，沒有變便宜" + (f"（之前 {fmtp(before)}）" if p != before else ""))
        s.update(last=p, lastAirline=best["airline"], lastDates=best["dates"], lastOut=best["out"], url=best["url"],
                 lastCheck=datetime.now(TW).strftime("%m/%d %H:%M"))
        if s.get("min") is None or p < s["min"]:
            s.update(min=p, minAt=datetime.now(TW).strftime("%m/%d %H:%M"), minAirline=best["airline"], minDates=best["dates"])
    # B. 新聞裡的特價航線
    routes = sale.get("routes") or []
    if routes:
        lines.append("\n<b>新聞裡的特價航線</b>（Google Flights 單程含稅最低價）")
        tomorrow = date.today() + timedelta(days=1)
        a, b = sale.get("travel") or [None, None]
        a = max(date.fromisoformat(a), tomorrow) if a else tomorrow + timedelta(days=14)
        b = date.fromisoformat(b) if b else a + timedelta(days=90)
        if b < a:
            b = a + timedelta(days=30)
        span = (b - a).days
        samples = sorted({a + timedelta(days=min(7, span)), a + timedelta(days=span // 2), b - timedelta(days=min(7, span))})
        core.出發地 = core.目的地 = ""
        core.只看這些航空公司 = [code] if code else []
        core.轉機, core.大人人數 = 0, 1
        core.去程最早出發 = core.去程最晚出發 = core.回程最早出發 = core.回程最晚出發 = None
        core.補抓完整清單 = core.補查沒價格的航班 = False
        for r in routes[:4]:
            best = cheapest_oneway(r["from"], r["to"], samples)
            news = f"新聞 {r['price']:,} 元起（未稅）" if r.get("price") else ""
            label = f"{html.escape(r.get('fromName', r['from']))} → {html.escape(r.get('toName', r['to']))}"
            if best:
                lines.append(f"・{label}：<b>{fmtp(best[0])}</b>（{dlabel(best[1])} {html.escape(best[2])}）　{news}")
            else:
                lines.append(f"・{label}：查不到（可能還沒更新、或被擋）　{news}")
        lines.append("\n<i>Google Flights 是含稅價，會比新聞的未稅價高；要輸入優惠碼的折扣這裡看不到。"
                     "便宜的座位很少，想搶的話直接到航空公司官網。</i>")
    if not mine and not routes:
        return
    send_text("\n".join(lines))


def main():
    only = os.environ.get("TRACK_ID") or ""
    tracks = load(TRACKS, [])
    state = load(STATE, {})
    now = datetime.now(TW)
    serp_budget(state)
    sale_raw = os.environ.get("SALE") or ""
    if sale_raw:
        try:
            sale_check(json.loads(sale_raw), tracks, state)
        finally:
            serp_done(state)
            save(STATE, state)
        return
    checked, failed = 0, 0
    for t in tracks:
        if only and t["id"] != only:
            continue
        if not t.get("active", True):
            continue
        s = state.setdefault(t["id"], {})
        if s.get("expired"):
            continue
        pairs = pairs_of(t)
        route = html.escape(route_of(t))
        if not pairs:
            s["expired"] = True
            send_text(f"⌛ <b>追蹤結束</b>：{route}\n{html.escape(dates_desc(t))}\n日期已經過了，之後不會再查。可以到查詢頁刪掉這一條。")
            continue
        print(f"▶ {route_of(t)}　{dates_desc(t)}　共 {len(pairs)} 組")
        setup_core(t)
        # 虎航官網（不受 Google 擋不擋影響，先查）
        tline = check_tiger(t, s, pairs, route, notify=(s.get("tiger") or {}).get("last") is not None)
        best = cheapest(t, pairs)
        checked += 1
        s["lastCheck"] = now.strftime("%m/%d %H:%M")
        if best is None:
            failed += 1
            s["fails"] = s.get("fails", 0) + 1
            if s["fails"] == 3:
                send_text(f"⚠️ <b>降價追蹤連續 3 次查詢失敗</b>：{route}\n可能被 Google 暫時擋住，之後會繼續自動再試。")
            continue
        if s.get("fails", 0) >= 3:
            send_text(f"✅ <b>降價追蹤恢復正常</b>：{route}")
        s["fails"] = 0

        price, last = best["price"], s.get("last")
        n = t.get("notify") or {"mode": "drop"}
        mode = n.get("mode", "drop")
        link = f"\n🔗 <a href=\"{html.escape(best['url'])}\">在 Google Flights 查看</a>"
        line = f"{html.escape(best['airline'])}　{best['dates']}　去程 {html.escape(best['out'])}"
        new_min = s.get("min") is None or price < s["min"]

        if last is None:
            send_text(f"👀 <b>開始追蹤</b>：{route}\n{html.escape(dates_desc(t))}\n\n目前最低：<b>{fmtp(price)}</b>（Google Flights 含稅）\n{line}\n"
                      + (f"\n{tline}\n" if tline else "") +
                      f"\n通知方式：{html.escape(notify_desc(n))}" + link)
        else:
            msg = None
            diff = price - last
            if mode == "below":
                target = int(n.get("target") or 0)
                if price <= target and (not s.get("alerted") or price < s.get("alertPrice", price + 1)):
                    msg = f"🎯 <b>低於目標價了！</b>\n{route}\n目標 {fmtp(target)}，現在 <b>{fmtp(price)}</b>"
                    s["alerted"], s["alertPrice"] = True, price
                elif price > target:
                    s["alerted"] = False
            elif mode == "dropBy":
                if -diff >= int(n.get("amount") or 0) and diff < 0:
                    msg = f"📉 <b>降價了！</b>\n{route}\n{fmtp(last)} → <b>{fmtp(price)}</b>（降 NT${-diff:,}）"
            elif mode == "any":
                if diff < 0:
                    msg = f"📉 <b>降價了！</b>\n{route}\n{fmtp(last)} → <b>{fmtp(price)}</b>（降 NT${-diff:,}）"
                elif diff > 0:
                    msg = f"📈 <b>漲價了</b>\n{route}\n{fmtp(last)} → <b>{fmtp(price)}</b>（漲 NT${diff:,}）"
            else:  # drop
                if diff < 0:
                    msg = f"📉 <b>降價了！</b>\n{route}\n{fmtp(last)} → <b>{fmtp(price)}</b>（降 NT${-diff:,}）"
            if msg:
                low = min(price, s.get("min") or price)
                send_text(msg + f"\n{html.escape(dates_desc(t))}\n{line}\n追蹤以來最低：{fmtp(low)}"
                          + ("　🆕 新低！" if new_min and s.get("min") is not None else "") + link)

        s.update(last=price, lastAirline=best["airline"], lastDates=best["dates"], lastOut=best["out"], url=best["url"])
        if new_min:
            s.update(min=price, minAt=now.strftime("%m/%d %H:%M"), minAirline=best["airline"], minDates=best["dates"])
        s.setdefault("history", []).append([now.strftime("%m/%d %H:%M"), price])
        s["history"] = s["history"][-60:]

    # 只保留還存在的追蹤（_ 開頭的是內部紀錄，例如 SerpApi 用量）
    serp_done(state)
    ids = {t["id"] for t in tracks}
    state = {k: v for k, v in state.items() if k in ids or k.startswith("_")}
    save(STATE, state)
    print(f"✓ 檢查 {checked} 條，失敗 {failed} 條")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
