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

SYSTEM = """你在幫一個「反指標」看板判讀台灣網紅 8zz（巴逆逆，粉專 DieWithoutBang）的臉書貼文。
她常分享自己的台股／ETF 操作與心情，市場把她當成反指標：看板會自動把她的看法反過來做，
所以你只要判斷「她本人」怎麼看，不要自己反過來。

## direction：她本人的看法
- "多"：她覺得會漲、在買、在加碼、很有信心、或套牢了但還在期待上漲。
- "空"：她覺得會跌、在賣、在停損減碼、買反向或 put、情緒崩潰想逃。
- "無"：跟股市無關（業配、乾爹贊助、生活、手錶、醫美、周邊商品、閒聊），或真的看不出方向。

她很少直接講「看多／看空」，常用反話、迷因和自創用語，請依她的習慣判讀：
- 偏多的說法：穩了、今天穩了、明天不會跌、大V天龍、衝啊、要V了、反攻第一天、抄底、接回來、認錯買回、
  一天100點台股5萬點、台股天下無敵、躲崩王、台股果然需要我坐鎮、我要成為買啥漲啥的福神、
  絕不停損、不要再賣了、拯救XX（＝買進某檔）、加入XX（＝買進）、槓桿拉高、換正二、買正二、
  好想買、等等就來接你、明天一定會漲、會再上去吧。
- 偏空的說法：人道走廊（＝趕快逃）、逃命、跑路、停損、減碼、降槓桿、收回資金、先閃、反彈就賣、
  又是反彈我不會被騙、一定是誘多、死貓跳、開高走低、要崩了、指數要墜機、世界末日、熊市、
  買反一／加碼反一、買 put、sell call、開期貨帳戶（＝想放空）、我不行了、給我個痛快、
  既然要爛就趕快到底、刪 app、不看盤了、存骨、去公園佔位置（＝虧到要睡公園）。
- 套牢哀嚎要分清楚：哀嚎但還在期待漲回（「就騙騙我也可以」「明天會漲吧」）算「多」；
  哀嚎到想認賠、想逃、絕望（「我不行了」「停損了啦」）算「空」。
- 注意反話：「我不會再被騙了，這一定是誘多」是空；「不要慌！只是技術性調整」是多。
- 買「反向」ETF（反1、00632R）＝ 空；買正2（00631L）、槓桿 ＝ 多。

## strength
1 ＝ 隨口一提、語氣模糊；2 ＝ 明確表態或有實際買賣；
3 ＝ 重押、梭哈、大額停損、借錢加碼、買反向／put、情緒極強烈（「世界末日」「天下無敵」「台股要崩了」）。

## ticker
她主要在講的標的，用 Yahoo Finance 代號（上市加 .TW、上櫃加 .TWO，例如 2330.TW）。
在講大盤、正二、反一、0050、或沒指定個股時一律填 "0050.TW"。

## 例子（實際貼文 → 判讀）
- 「今天穩了！大家一起跟我念！一天100點，台股5萬點！」→ 多 3
- 「看什麼看 今天就是反彈啦～ 妥妥開高走低 今天繼續看有什麼可以賣的」→ 空 2
- 「停損了啦 隨便了啦 股票就是騙人的爛東西 今天應該繼續探底」→ 空 3
- 「孩子別怕！我來了！我陪著大家一起飛！」→ 多 3
- 「今天是給你逃命的啊啊啊 我才不會進場啊啊啊！我加碼反一跟你說！」→ 空 3
- 「沒⋯沒事的 台股沒事的！不要被美國人騙了！」→ 多 2
- 「持續降低槓桿 剩下可能等12點之後再說了」→ 空 2
- 「早上起來抄底❤️ 把創意買回來 買正二補回來」→ 多 3
- 「其實好像⋯還好唉？難到是我錯了？」→ 無
- 「首先感謝乾爹 XX 的贊助～（產品介紹）」→ 無

## summary / reason
summary：8 個字以內的摘要，盡量用她的說法，例如「人道走廊」「台股五萬點」。
reason：一句話說明依據。只依貼文和附圖判斷，不要臆測貼文以外的資訊。"""

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


def load_images(urls, limit=2):
    """把附圖下載下來轉成 base64（臉書圖片網址會過期，不讓 API 自己去抓）。"""
    import base64
    out = []
    for u in urls[:limit]:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data, ctype = r.read(), r.headers.get_content_type()
            if ctype not in ("image/jpeg", "image/png", "image/gif", "image/webp") or len(data) > 4_000_000:
                continue
            out.append({"type": "image", "source": {"type": "base64", "media_type": ctype,
                                                     "data": base64.b64encode(data).decode()}})
        except Exception as exc:  # noqa: BLE001
            print(f"    附圖下載失敗，只看文字：{exc}")
    return out


def is_sponsored(post):
    """長篇業配文直接略過，不花 API 額度。短文裡提到乾爹的可能是在講股票，照樣判讀。"""
    t = post["text"]
    return len(t) > 250 and any(k in t for k in ("乾爹", "#AD", "#ad", "團購", "贊助"))


def classify(client, post):
    content = f"發文時間：{post['time']}\n\n貼文內容：\n{post['text'] or '（無文字）'}"
    if post["ocr"]:
        content += f"\n\n圖片中的文字（OCR）：\n{post['ocr']}"
    blocks = load_images(post.get("images", []))
    if blocks:
        content += "\n\n（附圖若是對帳單、下單畫面或走勢圖，請一併判讀標的與方向）"
    blocks.append({"type": "text", "text": content})
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        # 判讀是單純分類，用 low effort 省額度
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
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
        if is_sponsored(p):
            print(f"  {p['time']}  業配略過   {short[:40]}")
            continue
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
