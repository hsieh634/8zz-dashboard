#!/usr/bin/env python3
"""8zz 反指標看板：讀取 events.csv，從 Yahoo Finance 抓日線收盤價，輸出 dist/。

只用 Python 標準函式庫，GitHub Actions 上不需要 pip install。
回測邏輯在前端 (template.html) 計算，方便即時切換訊號門檻。
"""
import csv
import datetime as dt
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Taipei")
BENCHMARK = "0050.TW"
DEFAULT_TICKER = "0050.TW"
UA = "Mozilla/5.0 (8zz-dashboard)"


def load_events():
    events = []
    with open(ROOT / "events.csv", encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            t = (row.get("time") or "").strip()
            if not t:
                continue
            raw_dir = (row.get("direction") or "").strip().lower()
            if raw_dir in ("多", "偏多", "bull", "long", "+"):
                direction = "bull"
            elif raw_dir in ("空", "偏空", "bear", "short", "-"):
                direction = "bear"
            else:
                sys.exit(f"events.csv 第 {i} 行：direction 必須是 多 / 空，收到「{raw_dir}」")
            try:
                dt.datetime.strptime(t, "%Y-%m-%d %H:%M")
            except ValueError:
                sys.exit(f"events.csv 第 {i} 行：time 格式需為 YYYY-MM-DD HH:MM，收到「{t}」")
            events.append({
                "time": t,
                "dir": direction,
                "strength": max(1, min(3, int(row.get("strength") or 1))),
                "ticker": (row.get("ticker") or "").strip() or DEFAULT_TICKER,
                "summary": (row.get("summary") or "").strip(),
                "text": (row.get("text") or "").strip(),
                "url": (row.get("url") or "").strip(),
            })
    events.sort(key=lambda e: e["time"])
    return events


def twse_fill(stock_no, gaps):
    """Yahoo 偶爾缺某天收盤價，用證交所官方日成交資訊補上。"""
    out = {}
    for ym in sorted({g[:7] for g in gaps}):
        url = (f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json"
               f"&date={ym.replace('-', '')}01&stockNo={stock_no}")
        js = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    js = json.load(r)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    print(f"  證交所補資料失敗 {stock_no} {ym}：{exc}")
                time.sleep(3 * (attempt + 1))
        if js is None:
            continue
        for row in js.get("data", []):
            y, m, d = row[0].split("/")
            iso = f"{int(y) + 1911}-{m}-{d}"
            if iso in gaps:
                try:
                    out[iso] = float(row[6].replace(",", ""))
                except ValueError:
                    pass
        time.sleep(1)
    if out:
        print(f"  用證交所資料補了 {stock_no} 的 {', '.join(sorted(out))}")
    return out


def fetch_prices(ticker, start):
    p1 = int(dt.datetime.combine(start, dt.time(), TZ).timestamp())
    p2 = int(time.time()) + 86400
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
           f"?period1={p1}&period2={p2}&interval=1d")
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.load(r)["chart"]["result"][0]
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                raise RuntimeError(f"抓取 {ticker} 失敗：{exc}") from exc
            time.sleep(2 ** attempt)
    meta = res["meta"]
    off = meta.get("gmtoffset", 0)
    closes = res["indicators"]["quote"][0]["close"]
    bars, gaps = {}, set()
    for ts, c in zip(res.get("timestamp", []), closes):
        d = dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).strftime("%Y-%m-%d")
        if c is None:
            gaps.add(d)
        else:
            bars[d] = round(c, 4)
    gaps -= set(bars)
    if gaps and ticker.endswith(".TW") and not ticker.startswith("^"):
        bars.update(twse_fill(ticker[:-3], gaps))
    dates = sorted(bars)
    vals = [bars[d] for d in dates]
    return {
        "name": meta.get("shortName") or meta.get("longName") or ticker,
        "currency": meta.get("currency", ""),
        "dates": dates,
        "close": vals,
        "last_price": meta.get("regularMarketPrice"),
        "last_time": dt.datetime.fromtimestamp(meta.get("regularMarketTime", 0), TZ).strftime("%Y-%m-%d %H:%M"),
    }


def fetch_intraday(ticker, start):
    """盤中分 K：60 分 K 回溯到 start（Yahoo 上限 730 天），最近 60 天用 5 分 K。"""
    def get(q):
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?{q}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.load(r)["chart"]["result"][0]
        q0 = res["indicators"]["quote"][0]
        return [(t, o, c) for t, o, c in zip(res.get("timestamp", []), q0["open"], q0["close"]) if c is not None]

    p1 = int(dt.datetime.combine(max(start, dt.date.today() - dt.timedelta(days=720)), dt.time(), TZ).timestamp())
    try:
        h = get(f"period1={p1}&period2={int(time.time()) + 86400}&interval=60m")
        m5 = get("range=60d&interval=5m")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ {ticker} 抓不到盤中資料，改用收盤價（{exc}）")
        return None
    cut = m5[0][0] if m5 else float("inf")
    bars = [(t, 60, o, c) for t, o, c in h if t < cut] + [(t, 5, o, c) for t, o, c in m5]
    return {
        "t": [b[0] for b in bars],
        "m": [b[1] for b in bars],
        "o": [round(b[2] if b[2] is not None else b[3], 4) for b in bars],
        "c": [round(b[3], 4) for b in bars],
    }


def merge_cache(ticker, fresh):
    """Yahoo 的 5 分 K 只留 60 天，所以把抓過的分 K 存在 data/ 裡累積，同一時段以 5 分 K 為準。"""
    path = ROOT / "data" / f"intraday_{ticker.replace('^', '_')}.json"
    bars = {}
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        bars = {t: (m, o, c) for t, m, o, c in zip(old["t"], old["m"], old["o"], old["c"])}
    if fresh:
        for t, m, o, c in zip(fresh["t"], fresh["m"], fresh["o"], fresh["c"]):
            bars[t] = (m, o, c)
    if not bars:
        return None
    fine = sorted(t for t, v in bars.items() if v[0] == 5)
    first5 = fine[0] if fine else float("inf")
    keep = sorted(t for t, v in bars.items() if v[0] == 5 or t < first5)
    out = {"t": keep, "m": [bars[t][0] for t in keep], "o": [bars[t][1] for t in keep], "c": [bars[t][2] for t in keep]}
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    return out


def fill_from_intraday(daily, intra):
    """日 K 還缺的交易日，用當天最後一根分 K 的收盤價補。"""
    last = {}
    for t, c in zip(intra["t"], intra["c"]):
        last[dt.datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d")] = c
    have = set(daily["dates"])
    first = daily["dates"][0] if daily["dates"] else ""
    add = {d: c for d, c in last.items() if d not in have and d >= first}
    if not add:
        return
    bars = dict(zip(daily["dates"], daily["close"])) | add
    daily["dates"] = sorted(bars)
    daily["close"] = [bars[d] for d in daily["dates"]]
    print(f"  用盤中資料補了日 K：{', '.join(sorted(add))}")


def main():
    events = load_events()
    if not events:
        sys.exit("events.csv 沒有任何事件")
    start = dt.date.fromisoformat(events[0]["time"][:10]) - dt.timedelta(days=10)
    tickers = sorted({e["ticker"] for e in events} | {BENCHMARK})
    prices, intraday = {}, {}
    for t in tickers:
        try:
            prices[t] = fetch_prices(t, start)
        except Exception as exc:  # noqa: BLE001
            if t == BENCHMARK:
                raise
            print(f"⚠ {t}: 抓不到報價，這檔的事件會顯示為待進場（{exc}）")
            continue
        print(f"{t}: {len(prices[t]['dates'])} 根 K 棒，最新 {prices[t]['last_time']}")
        intra = merge_cache(t, fetch_intraday(t, start))
        if intra:
            intraday[t] = intra
            print(f"  盤中分 K {len(intra['t'])} 根")
            fill_from_intraday(prices[t], intra)

    data = {
        "generated_at": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "benchmark": BENCHMARK,
        "hold_bars": 17,
        "events": events,
        "prices": prices,
        "intraday": intraday,
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    tpl = (ROOT / "template.html").read_text(encoding="utf-8")
    page = tpl.replace("/*__DATA__*/null", payload)

    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "artifact.html").write_text(page, encoding="utf-8")
    (out / "index.html").write_text(
        '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        "<style>body{margin:0;font:14px system-ui,sans-serif}[hidden]{display:none!important}</style>"
        "</head><body>" + page + "</body></html>",
        encoding="utf-8",
    )
    print(f"完成：{len(events)} 筆事件 → dist/index.html")


if __name__ == "__main__":
    main()
