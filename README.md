# 學園偶像大師 (学マス) 聲優節目日曆 — Linux Docker 自架版

自動同步 `https://gakusei-cheer-portal.web.app/calendar` 的學園偶像大師聲優節目與活動行程，轉換為標準 iCalendar (`calendar.ics`)，並依「出演者」自動拆分為每位聲優的獨立訂閱日曆 (`casts/cast-*.ics`)。

本專案已完整改寫為 **Linux Docker 容器化架構**，內建多執行緒 Web 伺服器、背景定時同步排程、網頁手動更新／上傳介面，以及支援**繁體中文／簡體中文切換**的現代化前端網頁。

---

## ✨ 功能特色

- **🐳 一鍵 Docker 部署**：單一容器整合 Web 服務 + Playwright Chromium 無頭瀏覽器自動抓取 + 聲優日曆自動分流，非常適合部署在 Linux 小主機 / NAS / VPS 長期掛機運行。
- **🌐 繁體／簡體中文雙語網頁**：
  - 網頁右上角可隨時切換 **繁體中文** 與 **简体中文**（自動記憶語言偏好）。
  - 內建聲優與學マス角色名稱對照（如：`花岩 香奈` ↔ `葛城 莉莉婭 / 葛城 莉莉娅`），支援以聲優或角色名稱即時搜尋與篩選。
  - 提供「複製訂閱連結」、「系統日曆一鍵訂閱 (`webcal://`)」、「加入 Google 日曆」與「下載 `.ics`」按鈕。
- **🔄 自動排程 + 手動上傳雙保險**：
  - 容器背景預設每 3 小時自動抓取最新行程並重新拆分各聲優 `.ics`。
  - 若原站因 Firebase App Check 阻擋無頭瀏覽器，可直接透過網頁上的「📤 手動上傳 ICS」功能上傳 `calendar.ics`，伺服器會立即驗證並重新生成所有聲優獨立日曆。
- **🛡️ 訂閱網址永久固定**：
  - 每位聲優的檔案名稱由姓名 SHA-256 固定生成（如 `/casts/cast-658f49d479bba61d.ics`）。
  - 即使某位聲優暫時沒有未來行程，系統也會保留合法的空日曆，不會讓已訂閱的 Google / Apple 日曆出現 404 錯誤。

---

## 🚀 Linux Docker 快速部署

### 1. 使用 Docker Compose 啟動（推薦）

在您的 Linux 主機上取得本專案後，於專案根目錄執行：

```bash
docker compose up -d --build
```

啟動後：
- **公開訂閱主頁**：`http://<您的主機IP>:3002/`
- **系統管理後台 (Retrigger / 上傳 / 即時 Log)**：`http://<您的主機IP>:3002/admin`
- **聲優簡易表格頁**：`http://<您的主機IP>:3002/casts/`
- **總日曆訂閱網址**：`http://<您的主機IP>:3002/calendar.ics`
- **健康檢查端點**：`http://<您的主機IP>:3002/healthz`

> **資料持久化**：容器首次啟動時，會自動將初始的 `calendar.ics` 與 `casts/` 複製到宿主機的 `./data` 目錄中。之後所有自動更新或手動上傳的日曆都會保存在 `./data`，重啟或更新容器不會遺失任何資料。

---

### 2. 環境變數設定 (`docker-compose.yml` 或 `.env`)

您可以在 `docker-compose.yml` 或同目錄下的 `.env` 檔案調整以下參數：

| 變數名稱 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `PORT` | `3002` | Web 服務監聽埠號 |
| `AUTO_REFRESH` | `true` | 是否啟用背景定時自動更新 (`true` / `false`) |
| `REFRESH_ON_START` | `false` | 容器啟動時是否立即使用 Chromium 抓取一次遠端網站 |
| `ENABLE_PLAYWRIGHT_FETCH` | `true` | 是否啟用 Playwright 無頭瀏覽器抓取原站（若僅想手動上傳 `.ics` 可設為 `false`） |
| `REFRESH_INTERVAL_HOURS` | `3` | 背景自動同步間隔（小時） |
| `FETCH_MONTHS` | `18` | 每次往後查詢的月份數（預設 18 個月） |
| `BASE_URL` | *(空)* | 選填：固定外部公開網址（如 `https://cal.example.com`）。留空時會自動依瀏覽器請求的 `Host` / `X-Forwarded-Proto` 動態產生訂閱連結 |
| `ADMIN_TOKEN` | *(空)* | 選填：管理金鑰。設定後，網頁上的「立即同步更新」與「手動上傳 ICS」需輸入此金鑰才可執行 |

---

## 🔧 常用維運指令

```bash
# 查看容器即時日誌
docker compose logs -f

# 手動在容器內觸發一次遠端抓取與拆分
docker exec -it gakumas-calendar-ics python update_calendar.py --output /data/calendar.ics
docker exec -it gakumas-calendar-ics python split_casts.py --input /data/calendar.ics --output-dir /data/casts

# 運行單元測試
docker exec -it gakumas-calendar-ics python -m unittest discover -s tests -v
```

---

## 🌐 反向代理與外部訂閱建議

若您希望透過手機在戶外網路，或讓 **Google Calendar**（Google 伺服器需要能連線至您的網址）自動同步訂閱，建議在 Linux 小主機上搭配以下任一方式提供公開 HTTPS 網址：

1. **Cloudflare Tunnel (`cloudflared`)**：無須公網 IP 或開通路由器 Port，直接將 `http://localhost:3002` 對應至您的網域。
2. **Nginx / Caddy 反向代理**：將網域反向代理至 `127.0.0.1:3002`（支援自動讀取 `X-Forwarded-Proto` 與 `X-Forwarded-Host`）。
3. **Tailscale Funnel**：若使用 Tailscale，可直接開啟 Funnel 暴露 `3002` 埠供日曆服務抓取。

---

## 📁 專案結構

- [`server.py`](server.py)：Docker 容器主程式（HTTP 伺服器、REST API、背景定時排程、資料目錄初始化）。
- [`update_calendar.py`](update_calendar.py)：使用 Playwright Chromium 讀取原站日程並產生 RFC 5545 標準 `calendar.ics`。
- [`split_casts.py`](split_casts.py)：解析 `calendar.ics` 的 `出演者` 欄位，拆分為各聲優獨立 `.ics`、`casts/index.json` 與雙語 `casts/index.html`。
- [`index.html`](index.html)：支援繁體／簡體中文切換的公開日曆訂閱首頁。
- [`admin.html`](admin.html)：`/admin` 系統管理後台（手動觸發 Retrigger、手動上傳 `.ics`、Playwright 逐步即時日誌終端機）。
- [`Dockerfile`](Dockerfile) & [`docker-compose.yml`](docker-compose.yml)：Linux Docker 容器建置與編排設定。
- [`tests/`](tests/)：日曆序列化、聲優拆分與 HTTP 伺服器之自動化單元測試。
