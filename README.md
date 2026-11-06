# 8zz 反指標看板

把 8zz 的貼文情緒當反指標：她偏多 → 我方做空；她偏空 → 我方做多。
用各事件標的的實際收盤價（Yahoo Finance）回測，並和買入持有 0050 比較。

## 檔案

| 檔案 | 用途 |
| --- | --- |
| `events.csv` | 事件資料，一列一篇貼文（自動追蹤會自己往下加，你也可以手動改） |
| `fetch_fb.py` | 用 Apify 抓 8zz 粉專（DieWithoutBang）新貼文，交給 Claude 判讀多空、強度、標的，附圖也會一起看 |
| `seen_posts.json` | 已處理過的貼文 ID，避免重複 |
| `pending.csv` | 沒設 Claude 金鑰或 Claude 拒答時，新貼文會放這裡等你手動標記 |
| `examples/events.demo.csv` | 舊的示範資料 |
| `build.py` | 抓價格、產生 `dist/index.html`（只用 Python 標準函式庫） |
| `template.html` | 看板畫面與回測邏輯（Mode A / Mode B 都在前端計算） |
| `.github/workflows/update.yml` | 盤中每 20 分鐘自動重建並部署到 GitHub Pages |

## events.csv 欄位

```
time,direction,strength,ticker,summary,text,url
2026-07-20 09:59,多,3,0050.TW,虧損,FB 07/20 09:59 投的錢是3月的2倍…,https://www.facebook.com/...
```

- `time`：台北時間 `YYYY-MM-DD HH:MM`
- `direction`：`多` 或 `空`（指 8zz 本人的情緒，不是你的部位）
- `strength`：1–3 顆星
- `ticker`：Yahoo 代號，例如 `0050.TW`、`2330.TW`、`^TWII`、`00632R.TW`；空白 = `0050.TW`
- `summary` / `text` / `url`：顯示用

`direction` 是 8zz 本人的情緒：她看好、加碼、抄底 = 多；停損、買反向、哀嚎想逃 = 空。

## 本機執行

```bash
python3 build.py
```

```bash
open dist/index.html
```

## 部署到 GitHub Pages

1. 建一個 GitHub repo，把這個資料夾推上去（分支 `main`）。
2. Repo → Settings → Pages → Source 選 **GitHub Actions**。
3. 之後只要在 GitHub 網頁上編輯 `events.csv` 並 commit，看板就會自動重建；盤中也會每 20 分鐘更新價格。

## 自動追蹤 8zz 粉專

1. 到 https://apify.com 註冊（免費方案每月 5 美元額度），Settings → API & Integrations 複製 API token。
2. 到 https://console.anthropic.com 申請 API key（Claude 判讀貼文用）。
3. GitHub repo → Settings → Secrets and variables → Actions → New repository secret，
   新增 `APIFY_TOKEN` 和 `ANTHROPIC_API_KEY`。
4. 排程：盤中每 30 分鐘、每天 08:10 / 18:10 / 22:10 各一次。抓到新貼文會自動 commit 進 `events.csv` 並重建看板。

費用估計：Apify 每次約 0.02 美元，一個月約 5–6 美元；Claude 判讀一篇不到 0.01 美元。
想省錢就把 `.github/workflows/update.yml` 的 cron 改稀一點。

手動回補歷史貼文：

```bash
APIFY_TOKEN=你的token ANTHROPIC_API_KEY=你的key python3 fetch_fb.py --since 2026-01-01 --limit 500
```

注意：Apify 爬蟲偶爾會漏抓幾篇（同一時段抓兩次，回傳篇數可能不同），重要的貼文可以手動補進 `events.csv`。

## 回測規則

- 進場：貼文後第一個收盤價。台股標的 13:30 前發文用當日收盤，之後用下一交易日收盤。
- 翻轉：同一標的方向與前一筆不同。
- Mode B：翻轉時進場，持有到同一標的下一次反向訊號。
- Mode A：每篇貼文都進場，固定持有 17 根日 K。
- 資產曲線：每天把資金平均分給當天持有中的部位，按收盤價逐日計算，沒部位時持有現金；不計交易成本。
- 進出場落在同一根 K 棒的交易（例如半夜翻轉、隔天早上又翻轉）標為「同日翻轉」，不計勝負。
- 價格以 Yahoo Finance 為主，上市股票缺資料的日子用證交所官方日成交資訊補。
- 配色採台股慣例：紅色賺、綠色賠。
