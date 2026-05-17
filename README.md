# 凱基證券市府分公司 — 持股追蹤系統

每日盤後自動抓取證交所市場成交量前 100 大個股的分點明細（BSR），filter 出「**920D 凱基市府**」的進出資料，寫入 SQLite，並產生近 7 個交易日進出量 Top 10 的儀表板。

## 環境

- **Python 3.12.10**（`%LOCALAPPDATA%\Programs\Python\Python312`）
- 套件：`requests`、`ddddocr`、`beautifulsoup4`、`lxml`、`pillow`（已安裝）
- Windows Task Scheduler

## 檔案結構

```
kgi_cityhall_tracker/
├── backend/
│   ├── run_daily.py        # 主程式
│   ├── twse_top100.py      # 抓市場成交量前 N
│   ├── bsr_fetcher.py      # BSR 抓取 + CAPTCHA OCR
│   ├── db.py               # SQLite schema + query helpers
│   └── template.html       # Dashboard 模板
├── data/
│   └── tracker.sqlite      # 累積式資料庫
├── output/
│   └── dashboard.html      # 每日重新渲染
├── run_daily.bat
├── install_schedule.ps1
└── README.md
```

## 使用方式

### 手動執行

```cmd
run_daily.bat                       # 預設：top 100、開瀏覽器
run_daily.bat --top 30              # 只跑前 30 檔（測試用）
run_daily.bat --no-browser          # 不自動開啟瀏覽器
run_daily.bat --render-only         # 不重新抓取，只重畫儀表板
```

### 註冊每日自動執行（15:30）

```powershell
PowerShell -ExecutionPolicy Bypass -File install_schedule.ps1
```

排程操作：

```powershell
# 立即觸發（測試）
Start-ScheduledTask -TaskName 'KgiCityhallTracker_Daily'

# 查看狀態
Get-ScheduledTask -TaskName 'KgiCityhallTracker_Daily'

# 移除
Unregister-ScheduledTask -TaskName 'KgiCityhallTracker_Daily' -Confirm:$false
```

## 資料來源與技術說明

### 1. 市場成交量前 100
- 來源：證交所 `https://www.twse.com.tw/exchangeReport/MI_INDEX?type=ALLBUT0999`
- 無 CAPTCHA、純 JSON、約 1 秒完成
- 僅保留 4 位數股票代號（過濾 ETF/權證/特別股）

### 2. BSR 分點明細
- 來源：`https://bsr.twse.com.tw/bshtm/bsMenu.aspx`
- 每檔股票流程：
  1. GET menu page → 解析 `__VIEWSTATE`/`__EVENTVALIDATION`/CAPTCHA URL
  2. 下載 CAPTCHA → ddddocr 多模型 + 多影像變體投票 OCR
  3. POST 提交（含 `RadioButton_Normal=RadioButton_Normal`，缺此欄位會被靜默拒絕）
  4. 解析 result page 中的 `bsContent.aspx` 連結 → 抓 broker 表格
  5. Filter `凱基市府` 子字串並聚合
- 每檔最多 15 次重試；每檔間隔 2 秒節流

### 3. 分點名稱
- 凱基證券市府分公司在 BSR 中的字串為：`920D 凱基市府`
- 注意不要與 `9239 凱基市政`（市政分公司）混淆——`市府` 為精準子字串匹配

## SQLite Schema

| 表 | 用途 |
|---|---|
| `daily_top100` | 每日市場成交量前 100 名（候選池） |
| `kgi_cityhall_daily` | 凱基市府每日進出明細（已 filter） |
| `fetch_errors` | 抓取失敗紀錄（隔日重抓參考） |

## Dashboard 內容

- 標題列：產生時間、追蹤分點、資料庫累積天數
- 主表：近 7 天進出量 Top 10，含買進股數、賣出股數、淨買賣超、買入/賣出均價
- 每檔附 SVG sparkline：每日淨買賣超走勢
- 失敗清單：今日 OCR 或解析失敗的股票

## 已知限制

| 限制 | 影響 |
|---|---|
| 只掃市場前 100 大 | 凱基市府交易非熱門股會漏抓（實務影響小） |
| BSR 僅提供當日資料 | 歷史需逐日累積；可升級 FinMind 付費版回補過去資料 |
| CAPTCHA OCR 非 100% | 多模型投票 + 15 次重試後成功率 ≥ 99%，仍失敗則寫入 `fetch_errors`，隔天重抓 |

## 後續升級方向

1. **歷史回補**：FinMind 贊助會員（NT$590/月）一次下載過去 1-2 年全市場分點資料
2. **多分點追蹤**：schema 加入 `broker_id` 欄位，可同時追蹤多家分點
3. **分點群組偵測**：找出常與凱基市府同步進出的其他分點（疑似同一主力）
