# 智慧型文件分類歸檔 Telegram Bot

自動接收群組上傳的報告，透過 Gemini AI 分類與摘要，並歸檔至 Google Drive。

## 功能

- 支援 PDF、Word (.docx)、純文字 (.txt) 報告
- Gemini 2.5 Pro 自動分類 + 生成繁體中文 300 字摘要
- Google Drive 動態建立分類資料夾並歸檔
- 自動產生公開檢視連結回傳至 Telegram 群組

---

## 部署步驟

### 1. 建立 Telegram Bot

1. 在 Telegram 搜尋 `@BotFather`，發送 `/newbot`
2. 依指示設定 Bot 名稱，取得 **Bot Token**
3. 將 Bot 加入目標群組，並設定為管理員（或允許接收訊息）

---

### 2. 取得 Gemini API Key

1. 前往 [Google AI Studio](https://aistudio.google.com/)
2. 點擊右上角 **Get API key** → **Create API key**
3. 複製並保存 **API Key**

---

### 3. 建立 Google 服務帳戶

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立專案（或選擇既有專案）
3. 啟用 **Google Drive API**：
   - 左側選單 → **API 和服務** → **程式庫**
   - 搜尋 `Google Drive API` → 點擊 **啟用**
4. 建立服務帳戶：
   - 左側選單 → **API 和服務** → **憑證**
   - 點擊 **+ 建立憑證** → **服務帳戶**
   - 填入名稱後建立，角色選 **編輯者**
5. 下載金鑰 JSON：
   - 點擊剛建立的服務帳戶 → **金鑰** 分頁
   - **新增金鑰** → **建立新的金鑰** → 選 **JSON** → 下載
6. 記下 JSON 中的 `client_email`（格式如 `xxx@xxx.iam.gserviceaccount.com`）

---

### 4. 設定 Google Drive 根資料夾

1. 前往 [Google Drive](https://drive.google.com/)
2. 新建一個資料夾作為歸檔根目錄（例如：`報告歸檔`）
3. 右鍵該資料夾 → **共用**
4. 在「新增使用者」欄位貼上服務帳戶的 `client_email`，權限設為 **編輯者** → 送出
5. 取得資料夾 ID：
   - 開啟該資料夾，複製網址列中的 ID
   - 例：`https://drive.google.com/drive/folders/`**`1A2B3C4D5E6F7G8H`**
   - 粗體部分即為 **Folder ID**

---

### 5. 部署至 Railway

1. 將此專案推送至 GitHub repository

2. 前往 [Railway](https://railway.app/)，登入後點擊 **New Project** → **Deploy from GitHub repo**，選擇此 repository

3. Railway 會自動偵測 `Dockerfile` 並開始構建

4. 構建完成後，前往專案的 **Variables** 分頁，新增以下四個環境變數：

   | 變數名稱 | 說明 | 範例 |
   |----------|------|------|
   | `TELEGRAM_TOKEN` | 步驟 1 取得的 Bot Token | `7123456789:AAF...` |
   | `GEMINI_API_KEY` | 步驟 2 取得的 API Key | `AIzaSy...` |
   | `GOOGLE_DRIVE_PARENT_ID` | 步驟 4 取得的資料夾 ID | `1A2B3C4D5E6F7G8H` |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | 步驟 3 下載的 JSON 金鑰內容（**整個 JSON 壓縮成單行**） | `{"type":"service_account",...}` |

   > **如何將 JSON 壓縮成單行：**
   > ```bash
   > cat your-key-file.json | tr -d '\n'
   > ```
   > 複製輸出結果貼入 Railway Variables 即可。

5. 設定完成後，Railway 會自動重新部署。前往 **Logs** 分頁，看到以下訊息即代表成功：
   ```
   智慧型文件處理 Telegram 機器人啟動中...
   ```

---

## 使用方式

在已加入 Bot 的 Telegram 群組中，直接傳送 PDF、Word 或 TXT 檔案，Bot 將自動：

1. 下載並分析文件
2. 回傳分類標籤與 300 字摘要
3. 將原始文件歸檔至 Google Drive 對應分類資料夾
4. 附上可直接開啟的雲端檢視連結

---

## 限制說明

| 限制項目 | 上限 | 說明 |
|----------|------|------|
| 單檔大小 | 20 MB | Telegram Bot API 下載限制 |
| 支援格式 | PDF / DOCX / TXT | 其他格式會收到錯誤提示 |
| 同時處理數 | 2 個任務 | 由 Semaphore 控制，其餘排隊等待 |
| 摘要長度 | 250–300 字 | 由 Pydantic schema 強制約束 |

---

## 本地開發

```bash
# 安裝依賴
pip install -r requirements.txt

# 複製環境變數範本並填入實際值
cp .env.example .env

# 執行
python main.py
```

> 本地執行需安裝 [python-dotenv](https://pypi.org/project/python-dotenv/) 並在 `main.py` 頂部加入 `from dotenv import load_dotenv; load_dotenv()`，或直接在終端機 `export` 各環境變數。
