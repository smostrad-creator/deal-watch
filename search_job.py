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
def build_top_lines(R, keep):
    """依選定的航空公司，排出前 5 名的文字"""
    if R["kind"] == "scan":
        E = sorted((e for e in R["entries"] if e["airline"] in keep), key=lambda e: e["price"])
        return [f"{i}. {e['d1label']}～{e['d2label']}　{html.escape(e['airline'])}　<b>{fmtp(e['price'])}</b>"
                for i, e in enumerate(E[:5], 1)]
    best = sorted((x["best"], pr["label"].replace("\n", ""), a)
                  for pr in R["data"]["pairs"] for a, x in pr["airlines"].items()
                  if a in keep and x["best"] is not None)
    return [f"{i}. {lab}　{html.escape(a)}　<b>{fmtp(v)}</b>" for i, (v, lab, a) in enumerate(best[:5], 1)]


def excel_from_json(R, keep, path):
    """用存下來的結果，只留選定的航空公司，重新做一份 Excel（不用重新查詢）"""
    from datetime import date
    core.NOTES[:] = R.get("notes") or []
    p = R.get("params") or {}
    core.出發地, core.目的地 = p.get("origin", ""), p.get("dest", "")
    core.STOP_TXT = {"0": "直飛", "1": "最多轉 1 次"}.get(str(p.get("stops")), "可轉機")
    if R["kind"] == "scan":
        E = sorted((e for e in R["entries"] if e["airline"] in keep), key=lambda e: e["price"])
        top = [{**e, "retOk": True} for e in E[:10]]
        best = {}
        for e in E:
            best.setdefault(e["d1"], e)
        cal = [{**d, "price": best[d["d"]]["price"] if d["d"] in best else None,
                "airline": best[d["d"]]["airline"] if d["d"] in best else ""} for d in R["dayList"]]
        core.write_scan_excel(top, cal, path)
        return
    results, pairs = [], []
    for pr in R["data"]["pairs"]:
        if "d1" not in pr:
            raise RuntimeError("這次的結果太舊，請重新查詢一次")
        outs, rets, rel, picked = {}, {}, {}, {}
        for a, x in pr["airlines"].items():
            if a not in keep:
                continue
            outs[a] = [{"text": o["text"], "price": o["price"]} for o in x["outs"]]
            rets[a] = [{"text": r["text"], "price": r["price"]} for r in x["rets"]]
            rel[a] = x.get("reliable", True)
            star = [o["text"] for o in x["outs"] if o.get("star")]
            if star:
                picked[a] = star[0]
        results.append((outs, rets, rel, picked, [], pr["url"]))
        pairs.append((date.fromisoformat(pr["d1"]), date.fromisoformat(pr["d2"])))
    core.write_excel(results, pairs, path)


def resend(p):
    """查詢頁選好航空公司後：用上次的結果，只留選的航空，重新傳到 Telegram（不重新查詢）"""
    path = os.path.join(OUT, "latest.json")
    R = json.load(open(path, encoding="utf-8"))
    if R.get("reqId") != p.get("reqId"):
        send_text("⚠️ 這份結果已經被新的查詢取代，請在查詢頁重新選一次航空公司。")
        return
    keep = list(p.get("airlines") or [])
    P = R.get("params") or {}
    lines = build_top_lines(R, set(keep))
    try:
        excel_from_json(R, set(keep), os.path.join(OUT, "latest.xlsx"))
        xok = True
    except Exception as e:
        print("Excel 重做失敗：", e)
        xok = False
    page = os.environ.get("PAGES_URL", "")
    notes = R.get("notes") or []
    first = not R.get("sentAirlines")
    allnames = [a["name"] for a in R.get("summary") or []]
    pick_txt = "全部航空" if set(keep) >= set(allnames) else "、".join(keep)
    if first and notes:
        send_text("🚨 <b>注意！有航空公司查不到價格</b>\n這些航空有飛，但 Google 沒給價格，<b>沒有列進比價表</b>，可能更便宜：\n\n" +
                  "\n".join("⚠️ <b>" + html.escape(n.split("：")[0]) + "</b>" for n in notes) +
                  "\n\n請到 Google Flights 查看價格。")
    head = (f"✈️ <b>{html.escape(route_name(P))}　查詢結果</b>" if first
            else f"🔄 <b>{html.escape(route_name(P))}　已依你選的航空更新</b>")
    fail = R.get("failNote") or ""
    msg = (head + f"\n只看：{html.escape(pick_txt)}\n\n" +
           (f"⚠️ {html.escape(fail)}\n\n" if fail else "") +
           ("\n".join(lines) if lines else "選的航空公司沒有有價格的航班") +
           (f"\n\n⚠️ 另有 {len(notes)} 家航空查不到價格，沒有列入" + ("（見上一則）" if first else "") if notes else "") +
           ("\n\n📊 完整比價表：打開查詢頁看「最近結果」"))
    send_text(msg)
    if xok:
        send_file(os.path.join(OUT, "latest.xlsx"), "比價 Excel")
    R["sentAirlines"] = keep
    with open(path, "w", encoding="utf-8") as f:
        json.dump(R, f, ensure_ascii=False)
    print("✓ 已傳送")


def main():
    p = json.loads(os.environ.get("SEARCH_PARAMS") or "{}")
    if p.get("action") == "resend":
        return resend(p)
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
            for pr, (d1, d2) in zip(data["pairs"], web.cache.get("pairs", [])):
                pr["d1"], pr["d2"] = d1.isoformat(), d2.isoformat()
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

        fail = getattr(core, "FAIL_NOTE", "") or ""
        serp = getattr(core, "SERP_USED", 0)
        serp_txt = f"ℹ️ 有 {serp} 組被 Google 擋住，改用備用來源（SerpApi）查到。\n\n" if serp else ""
        out.update(notes=notes, file=xfile, error=None, failNote=fail, serpUsed=serp)
        names = [a["name"] for a in out.get("summary") or []]
        if len(names) > 1:
            # 先不傳結果：等使用者在查詢頁選好航空公司，按「確定」後才傳（只傳一次）
            with open(os.path.join(OUT, "latest.json"), "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
            send_text(f"✅ <b>{html.escape(route_name(p))}　查完了</b>\n"
                      f"找到 {len(names)} 家航空公司" + (f"，另有 {len(notes)} 家查不到價格" if notes else "") + "。\n\n"
                      + (f"⚠️ {html.escape(fail)}\n\n" if fail else "") + serp_txt
                      + "👉 請回到查詢頁，選擇要比較的航空公司。\n"
                      + "選好後，才會傳完整結果和 Excel 給你。")
            print("✓ 完成，等使用者選航空公司")
            return
        out["sentAirlines"] = names
        with open(os.path.join(OUT, "latest.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)

        # Telegram：沒價格的警告先傳，比較顯眼
        if notes:
            send_text("🚨 <b>注意！有航空公司查不到價格</b>\n這些航空有飛，但 Google 沒給價格，<b>沒有列進比價表</b>，可能更便宜：\n\n" +
                      "\n".join("⚠️ <b>" + html.escape(n.split("：")[0]) + "</b>" for n in notes) +
                      "\n\n請到 Google Flights 查看價格。")
        msg = (f"✈️ <b>{html.escape(route_name(p))}　查詢完成</b>\n{head}\n\n" +
               (f"⚠️ {html.escape(fail)}\n\n" if fail else "") + serp_txt +
               ("\n".join(lines) if lines else "沒有找到有價格的航班") +
               (f"\n\n⚠️ 另有 {len(notes)} 家航空查不到價格（見上一則）" if notes else "") +
               ("\n\n📊 完整比價表：打開查詢頁看「最近結果」"))
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
