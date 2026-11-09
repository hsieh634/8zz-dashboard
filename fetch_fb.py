#!/usr/bin/env python3
"""自動追蹤 8zz（巴逆逆）粉專新貼文 → 用 Claude 判讀多空 → 寫進 events.csv。

需要的環境變數：
  APIFY_TOKEN        Apify API token（用 apify/facebook-posts-scraper 抓粉專貼文）
  ANTHROPIC_API_KEY  Claude API key（判讀多空、強度、標的）。沒設的話，新貼文會放進
                     pending.csv 等你手動標記，不會寫進 events.csv。

用法：
  python3 fetch_fb.py                    # 抓上次之後的新貼文
  python3 fetch_fb.py --since 2026-01-01 # 回補某日之後的所有貼文
  python3 fetch_fb.py --dry-run          # 只印出結果，不寫檔
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Taipei")
PAGE_URL = "https://www.facebook.com/DieWithoutBang/"
STATE = ROOT / "seen_posts.json"
EVENTS = ROOT / "events.csv"
PENDING = ROOT / "pending.csv"
FIELDS = ["time", "direction", "strength", "ticker", "summary", "text", "url"]
MODEL = "claude-opus-5-5"

SYSTEM = """你在幫一個「反指標」看板判讀台灣網紅 8zz（巴逆逆）的臉書貼文。
她常分享自己的台股／ETF 操作與心情，市場上把她的看法當成反指標。

請判斷這篇貼文透露的「她本人」對市場或特定標的的情緒：
- direction：
  - "多"：她看好、加碼、買進、抄底、滿倉、覺得會漲、賺錢很開心而想續抱。
  - "空"：她看壞、停損、認賠、賣出、放空、買反向 ETF、哀嚎虧損想逃、覺得會跌。
  - "無"：跟股市無關（生活、業配、閒聊），或看不出方向。
  - 注意：買「反向」ETF（如 00632R）＝ 她看空；買正 2（如 00631L）＝ 她看多。
- strength：1＝隨口一提，2＝明確表態，3＝重押、情緒強烈、梭哈或停損大賠。
- ticker：她主要在講的標的，用 Yahoo Finance 代號（上市加 .TW，上櫃加 .TWO，
  例如 2330.TW、0050.TW、00632R.TW；整體大盤用 ^TWII）。沒指定個股就填 "0050.TW"。
  若她買的是反向／槓桿 ETF，ticker 填它追蹤的原型標的（大盤用 ^TWII），方向依她對大盤的看法。
- summary：8 個字以內的情緒摘要，例如「停損哀嚎」「抄底加碼」。
- reason：一句話說明你的判斷依據。
只依貼文內容判斷，不要臆測貼文以外的資訊。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["多", "空", "無"]},
        "strength": {"type": "integer", "enum": [1, 2, 3]},
        "ticker": {"type": "string"},
        "summary": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["direction", "strength", "ticker", "summary", "reason"],
    "additionalProperties": False,
}


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"last_time": None, "seen": []}


def fetch_posts(token, since, limit):
    body = {"startUrls": [{"url": PAGE_URL}], "resultsLimit": limit, "captionText": True}
    if since:
        body["onlyPostsNewerThan"] = since
    req = urllib.request.Request(
        "https://api.apify.com/v2/acts/apify~facebook-posts-scraper/run-sync-get-dataset-items",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = json.load(r)
    posts = []
    for it in raw:
        if it.get("error"):
            continue
        media = it.get("media") or []
        ocr = "\n".join(m.get("ocrText", "") for m in media
                        if m.get("ocrText") and not m["ocrText"].startswith("May be"))
        images = [u for m in media
                  for u in [(m.get("photo_image") or {}).get("uri") or m.get("thumbnail")] if u][:4]
        ts = it.get("time") or it.get("timestamp")
        if isinstance(ts, (int, float)):
            when = dt.datetime.fromtimestamp(ts, TZ)
        else:
            when = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(TZ)
        posts.append({
            "id": str(it.get("postId") or it.get("id") or it.get("url")),
            "time": when.strftime("%Y-%m-%d %H:%M"),
            "text": (it.get("text") or it.get("message") or "").strip(),
            "ocr": ocr.strip(),
            "url": it.get("url") or it.get("topLevelUrl") or "",
            "images": images,
        })
    posts.sort(key=lambda p: p["time"])
    return posts


def classify(client, post):
    content = f"發文時間：{post['time']}\n\n貼文內容：\n{post['text'] or '（無文字）'}"
    if post["ocr"]:
        content += f"\n\n圖片中的文字（OCR）：\n{post['ocr']}"
    blocks = [{"type": "image", "source": {"type": "url", "url": u}} for u in post.get("images", [])]
    blocks.append({"type": "text", "text": content + "\n\n（附圖若是對帳單或下單畫面，請一併判讀標的與方向）"})
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": blocks}],
    )
    if resp.stop_reason == "refusal":
        return None
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def append_rows(path, rows, fields):
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="只抓這個時間之後的貼文（YYYY-MM-DD 或 Apify 相對時間如 '2 days'）")
    ap.add_argument("--limit", type=int, default=10, help="最多抓幾篇（預設 10）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit("缺少 APIFY_TOKEN")

    state = load_state()
    seen = set(state["seen"])
    if args.since:
        since = args.since
    elif state["last_time"]:
        # Apify 每回傳一篇就收費，所以只抓上次最後一篇之後的時段（相對時間，多抓 1 小時當緩衝）
        last = dt.datetime.strptime(state["last_time"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        hours = max(2, int((dt.datetime.now(TZ) - last).total_seconds() // 3600) + 1)
        since = f"{hours} hours"
    else:
        since = "3 days"
    posts = [p for p in fetch_posts(token, since, args.limit) if p["id"] not in seen]
    print(f"抓到 {len(posts)} 篇新貼文（since={since}）")
    if not posts:
        return

    client = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()

    events, pending = [], []
    for p in posts:
        text = p["text"] or p["ocr"]
        short = text.replace("\n", " ")[:160]
        r = classify(client, p) if client else None
        if r is None:
            pending.append({"time": p["time"], "direction": "?", "strength": "", "ticker": "",
                            "summary": "", "text": short, "url": p["url"]})
            print(f"  {p['time']}  待人工判讀  {short[:40]}")
        elif r["direction"] == "無":
            print(f"  {p['time']}  無關       {short[:40]}（{r['reason']}）")
        else:
            events.append({"time": p["time"], "direction": r["direction"], "strength": r["strength"],
                           "ticker": r["ticker"] or "0050.TW", "summary": r["summary"],
                           "text": short, "url": p["url"]})
            print(f"  {p['time']}  {r['direction']} {'★' * r['strength']}  {r['ticker']}  {r['summary']}（{r['reason']}）")

    if args.dry_run:
        return
    if events:
        append_rows(EVENTS, events, FIELDS)
    if pending:
        append_rows(PENDING, pending, FIELDS)
    state["seen"] = sorted(seen | {p["id"] for p in posts})[-2000:]
    state["last_time"] = max([state["last_time"] or ""] + [p["time"] for p in posts])
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"寫入 {len(events)} 筆事件、{len(pending)} 筆待判讀")


if __name__ == "__main__":
    main()
