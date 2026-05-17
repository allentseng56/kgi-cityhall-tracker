# 雲端部署指南（GitHub Actions + Pages）

把這個專案推上 GitHub 後，會：
- 每個交易日台北時間 **15:30 自動執行**（GitHub Actions cron）
- 抓取結果寫入 `data/tracker.sqlite`，自動 commit 回 repo
- Dashboard 部署到 **GitHub Pages**，你隨時打開固定網址就能看最新解讀
- **完全免費**（公開 repo）

---

## 你需要做的事（一次性，約 10 分鐘）

### 1. 建立 GitHub 帳號（若無）
到 https://github.com/ 註冊。

### 2. 在本機初始化 git 並推上 GitHub

打開 PowerShell，cd 到專案目錄：

```powershell
cd C:\Users\andre\.claude\sessions\投資理財\kgi_cityhall_tracker
git init -b main
git add .
git commit -m "initial: KGI city-hall tracker"
```

到 GitHub 網頁建立**新的公開 repo**（建議命名 `kgi-cityhall-tracker`）。**不要勾選**「Initialize with README」等選項。

接著把本地 repo 推上去（把 `YOUR_USERNAME` 換成你的 GitHub 帳號）：

```powershell
git remote add origin https://github.com/YOUR_USERNAME/kgi-cityhall-tracker.git
git push -u origin main
```

### 3. 啟用 GitHub Pages

到 repo 頁面 → **Settings** → 左側 **Pages**：
- Source 選 **Deploy from a branch**
- Branch 選 **main**，資料夾選 **/docs**
- 按 **Save**

幾秒後 GitHub 會給你 dashboard 網址：
```
https://YOUR_USERNAME.github.io/kgi-cityhall-tracker/
```

### 4. 給 Actions 寫入權限

到 **Settings → Actions → General** → 滑到底：
- **Workflow permissions** 選 **Read and write permissions**
- **Allow GitHub Actions to create and approve pull requests** 勾起
- 按 **Save**

### 5. 手動觸發第一次執行

到 repo 頁面 → **Actions** tab → 點左側 **Daily BSR Fetch** → 右上 **Run workflow** → 綠色 Run workflow 按鈕。

約 10 分鐘後跑完，repo 會自動 commit 新的 `data/tracker.sqlite` 和 `docs/index.html`。重新整理 Pages 網址就看到 dashboard。

### 6. 之後完全自動

接下來每個工作日 15:30（台北時間）會自動執行。你只要打開 Pages 網址就能看當日結果。

---

## 常見問題

### Q: 為什麼是公開 repo？
A: 公開 repo 才能用 GitHub Pages 免費版，且我們的資料來源（TWSE BSR）本身就是公開資訊，沒有隱私問題。如果你堅持私有 repo，需要付 GitHub Pro（$4/月）才能用 Pages。

### Q: 如何更改執行時間？
A: 編輯 `.github/workflows/daily.yml`，修改 cron 表達式。`30 7 * * 1-5` 表示 UTC 07:30 週一到五，即台北 15:30。例如改成 16:00 就是 `0 8 * * 1-5`。

### Q: 跑失敗了怎麼辦？
A: 到 **Actions** tab 看 log。常見原因：
- BSR CAPTCHA OCR 連續失敗 → 重新觸發即可
- GitHub Actions IP 被擋 → 暫時換 IP 通常隔天恢復
- TWSE 維護 → 等下個交易日

### Q: SQLite 一直長大怎麼辦？
A: 每天約 30-50 KB 增量，一年約 10-15 MB，不會有問題。真正需要時可以開個 Issue 提醒清理。

### Q: 想加 Telegram / Email 通知？
A: 在 workflow 最後加一個 step 呼叫 webhook 或 email API。可以後續再加。

---

## 本地手動執行也仍然可以

雲端設定後，本地的 `run_daily.bat` 仍可使用——適合臨時想立刻抓的情境。本地與雲端共用同一份 SQLite 結構，差別只是 SQLite 檔案不同（本地一份、雲端 repo 一份）。
