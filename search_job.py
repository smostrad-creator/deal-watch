# -*- coding: utf-8 -*-
"""
手機查機票：由 GitHub Actions 執行。
讀取設定頁送來的查詢條件（環境變數 SEARCH_PARAMS），用電腦版同一套程式查詢，
結果存成 docs/results/latest.json 給查詢頁顯示，並把摘要和 Excel 傳到 Telegram。
"""
import html
import json
import os
import shutil
import sys
import traceback
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "docs", "results")
TW = timezone(timedelta(hours=8))

sys.path.insert(0, HERE)
import 查機票網頁 as web  # noqa: E402  電腦版的網頁程式（裡面有完整的查詢流程）

core = web.core
core.print = lambda *a, **k: print(*a, flush=True)
web.log = lambda *a, **k: print(*a, flush=True)
core.should_stop = lambda: False


# ─────────────── Telegram ───────────────
def tg(method, data=None, files=None):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("（沒有 TELEGRAM_TOKEN，略過傳送）")
        return
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in (data or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        for k, (name, content) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{name}\"\r\n"
                     "Content-Type: application/octet-stream\r\n\r\n").encode() + content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        print("Telegram 傳送失敗：", e)


def send_text(text):
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    tg("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})


def send_file(path, caption=""):
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    with open(path, "rb") as f:
        tg("sendDocument", {"chat_id": chat, "caption": caption}, {"document": (os.path.basename(path), f.read())})


def fmtp(n):
    return f"NT${n:,}" if n is not None else "未提供"


def route_name(p):
    a = p.get("originLabel") or p["origin"]
    b = p.get("destLabel") or p["dest"]
    return f"{a} ⇄ {b}"


# ─────────────── 主程式 ───────────────
def main():
    p = json.loads(os.environ.get("SEARCH_PARAMS") or "{}")
    req = p.get("reqId", "")
    page = os.environ.get("PAGES_URL", "")
    os.makedirs(OUT, exist_ok=True)
    out = {"reqId": req, "created": datetime.now(TW).strftime("%Y-%m-%d %H:%M"), "params": p}
    print("查詢條件：", json.dumps(p, ensure_ascii=False))

    try:
        web.run_search(p)
        st = web.state
        if st["error"]:
            raise RuntimeError(st["error"])
        if st.get("ask") and st["ask"]["kind"] == "scan":
            web.scan_phase2([a["name"] for a in st["ask"]["airlines"]], inline=True)
            if st["error"]:
                raise RuntimeError(st["error"])
        notes = list(dict.fromkeys(core.NOTES))
        ol, dl = p.get("originLabel") or p["origin"], p.get("destLabel") or p["dest"]
        notes = [n.replace(f"{p['origin']} → {p['dest']}", f"{ol} → {dl}").replace(f"{p['dest']} → {p['origin']}", f"{dl} → {ol}")
                 for n in notes]
        res = st["result"]
        if res is None:
            raise RuntimeError("沒有查到任何結果")

        # Excel 搬到 docs/results
        xfile = None
        if st.get("file") and os.path.exists(os.path.join(HERE, st["file"])):
            xfile = "latest.xlsx"
            shutil.move(os.path.join(HERE, st["file"]), os.path.join(OUT, xfile))

        if res.get("kind") == "scan":
            # 把每天、每家的最低價都存下來，查詢頁可以自己依航空公司重新排名
            rets = {(t["d1"], t["airline"]): t["ret"] for t in res["top"] if t.get("ret")}
            entries = [{"d1": e["d1"].isoformat(), "d1label": web.fmt_day(e["d1"]), "d2label": web.fmt_day(e["d2"]),
                        "airline": e["airline"], "price": e["price"], "out": e["o"]["text"],
                        "ret": rets.get((e["d1"].isoformat(), e["airline"]), ""), "url": e["ctx"]["url"]}
                       for e in web.cache.get("entries", [])]
            days = [{"d": d1.isoformat(), "d1label": web.fmt_day(d1), "d2label": web.fmt_day(d2), "url": url}
                    for d1, d2, url in web.cache.get("day_list", [])]
            summary = web.airline_summary([(e["airline"], e["price"], e["d1"]) for e in entries])
            out.update(kind="scan", days=res["days"], entries=entries, dayList=days, summary=summary)
            top = sorted(entries, key=lambda e: e["price"])[:5]
            lines = [f"{i}. {e['d1label']}～{e['d2label']}　{html.escape(e['airline'])}　<b>{fmtp(e['price'])}</b>"
                     for i, e in enumerate(top, 1)]
            head = f"每天出發、玩 {res['days']} 天，共 {len(days)} 個出發日"
        else:
            data = res
            out.update(kind="normal", data=data,
                       summary=st["ask"]["airlines"] if st.get("ask") else
                       web.airline_summary([(a, x["best"], i) for i, pr in enumerate(data["pairs"])
                                            for a, x in pr["airlines"].items() if x["best"] is not None]))
            best = []
            for pr in data["pairs"]:
                for a, x in pr["airlines"].items():
                    if x["best"] is not None:
                        best.append((x["best"], pr["label"].replace("\n", ""), a))
            best.sort()
            lines = [f"{i}. {lab}　{html.escape(a)}　<b>{fmtp(pv)}</b>" for i, (pv, lab, a) in enumerate(best[:5], 1)]
            head = f"共 {len(data['pairs'])} 組日期"

        out.update(notes=notes, file=xfile, error=None)
        with open(os.path.join(OUT, "latest.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)

        # Telegram：沒價格的警告先傳，比較顯眼
        if notes:
            send_text("🚨 <b>注意！有航空公司查不到價格</b>\n這些航空有飛，但 Google 沒給價格，<b>沒有列進比價表</b>，可能更便宜：\n\n" +
                      "\n".join("⚠️ <b>" + html.escape(n.split("：")[0]) + "</b>" for n in notes) +
                      "\n\n請到 Google Flights 查看價格。")
        msg = (f"✈️ <b>{html.escape(route_name(p))}　查詢完成</b>\n{head}\n\n" +
               ("\n".join(lines) if lines else "沒有找到有價格的航班") +
               (f"\n\n⚠️ 另有 {len(notes)} 家航空查不到價格（見上一則）" if notes else "") +
               (f"\n\n📊 <a href=\"{page}#results\">打開完整比價表</a>" if page else ""))
        send_text(msg)
        if xfile:
            send_file(os.path.join(OUT, xfile), "完整比價 Excel")
        print("✓ 完成")
    except Exception as e:
        traceback.print_exc()
        out.update(error=str(e))
        with open(os.path.join(OUT, "latest.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        send_text(f"❌ <b>查詢失敗</b>：{html.escape(route_name(p)) if p.get('origin') else ''}\n{html.escape(str(e))}")


if __name__ == "__main__":
    main()
