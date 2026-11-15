# 📚 國立東華大學圖書館 AI 館藏搜尋與拿書路徑規劃 Agent

本專案為「網路實驗」課程小專題，結合 **Google Gemini API（Function Calling & 多模態理解）**、**Playwright 動態網頁爬蟲** 與 **Streamlit 透明化互動介面**，打造專屬於國立東華大學圖書館（壽豐校區圖資大樓）的智慧館員助理。

使用者只需輸入自然語言找書需求（或上傳 **PDF / 圖片格式之課程大綱**），系統即可自動拆解核心關鍵字、檢索東華 OPAC 真實「在架」館藏、補齊外部書目簡介，並依館藏樓層與索書號規劃「由高樓層到低樓層、不走回頭路」的最省力取書路徑。

---

## ✨ 核心功能亮點

1. **自主決策與並行工具調用（Agentic Workflow）**
   * 將自然語言條件（如出版年份、在架狀態、主題限制）自動拆解為多個精簡關鍵字，並行查詢東華 OPAC 館藏。
   * 具備 **自主放寬條件重試機制**：當冷門關鍵字查無館藏時，自動移除年份限制或改用上位詞重試；若仍無館藏，則主動建議改用「全國文獻傳遞服務系統（NDDS）」。
2. **多模態課程大綱（Syllabus）解析**
   * 支援直接上傳 **`.pdf` 課程大綱檔案** 或 **`.png` / `.jpg` 書單截圖**，由 Gemini 原生解析指定教科書與參考書目，並自動對接東華館藏系統確認借閱狀態。
3. **跨樓層最省力拿書路徑規劃（Smart Shelf Routing）**
   * 內建東華圖書館索書號（中文賴永祥分類法 `000–999` 與西文 LCC 分類法 `A–Z`）樓層對照規則庫，自動將推薦書籍依 **「5 樓 $\rightarrow$ 4 樓 $\rightarrow$ 3 樓 $\rightarrow$ 2 樓」** 排序，並標記「限館內閱覽（參考書）」防呆提醒。
4. **雙重外部書目摘要與本地快取**
   * 串接 **Google Books API**（主）與 **博客來搜尋頁**（副）補齊書籍內容簡介，並將查詢結果存入本地 **SQLite（`library_cache.db`）**，實現毫秒級快取命中（Cache Hit）。

---

## 🛡️ Responsible AI（負責任的 AI）設計規範

本系統從架構設計階段即落實四大 Responsible AI 原則：

* **🔍 透明度與可解釋性（Transparency & Explainability）**
  * 前端透過 `st.status` 與 JSON 軌跡面板，即時公開 Agent 拆解的關鍵字、呼叫的工具與參數，並於每本書旁附上 **東華 OPAC 原始館藏詳情頁直達連結** 供人工查核（Human-in-the-loop）。
* **🎯 抑制幻覺與誠實邊界（Anti-Hallucination & Honesty）**
  * 模型推論溫度設為 `temperature = 0.1`，嚴格規範書名、作者、索書號與在架狀態必須 100% 源自爬蟲工具回傳值。
  * 當冷門學術書在外部資料庫查無公開目錄時，系統回傳 `inferred_from_subject` 標記，並強制在介面上顯示警語：**「⚠️ 此書無外部公開目錄，以下簡介為 AI 依據書名與館藏主題推估，借閱前請以實體書為準」**。
* **🔒 隱私保護與資安邊界（Privacy & Security）**
  * 明確劃清資安邊界，**絕不要求、不儲存任何使用者的東華單一簽入（SSO）學號與密碼**，預約一律導向官方頁面自行登入完成；所有 API 金鑰透過 `.env` 載入並由 `.gitignore` 隔離。
* **🚦 爬蟲倫理與系統高可用性（Ethical Scraping & Guardrails）**
  * 使用 `threading.Semaphore(2)` 限制同時連線數並搭配 SQLite 本地快取，大幅減少對學校圖書館伺服器的重複請求。
  * 設定 `MAX_TOOL_TURNS = 3` 單次對話工具呼叫上限防止無窮迴圈，並內建多模型自動降級切換（Fallback）應對 API 尖峰負載。

---

## 🏗️ 系統架構與目錄結構

```text
ndhu-library-agent/
├── .env                  # 環境變數檔（存放 GEMINI_API_KEY，已列入 .gitignore）
├── .gitignore            # 排除 .env、*.db、venv/、mock_html/ 等本地檔案
├── README.md             # 專案說明文件
├── opac_crawler.py       # 【成員 A】東華 OPAC Playwright 爬蟲、Regex 清洗、樓層字典與 SQLite 快取
├── agent_core.py         # 【成員 B】Gemini Agent 主迴圈、多模態解析、外部摘要管線與安全護欄
└── app.py                # 【成員 C】Streamlit 透明化前端介面、一鍵情境 Demo 與 Markdown 匯出
```

### 👥 團隊分工一覽

| 成員 | 負責模組 | 核心實作內容 |
| :--- | :--- | :--- |
| **成員 A** | `opac_crawler.py` | 東華 ToRead OPAC 動態網頁解析、民國年／ISBN 正規表達式清洗、館藏狀態白名單過濾、東華樓層對照規則與 SQLite 快取。 |
| **成員 B** | `agent_core.py` | `google-genai` Function Calling 調度、並行關鍵字檢索、PDF／圖片多模態處理、Google Books＋博客來雙重摘要備援與防幻覺護欄。 |
| **成員 C** | `app.py` | Streamlit 對話介面設計、`st.status` 即時思考軌跡可視化、Responsible AI 治理聲明面板與 Markdown 筆記匯出。 |

---

## 🚀 快速開始與環境建置

### 1. 複製專案並建立虛擬環境
```bash
git clone <你的GitHub倉庫網址>
cd ndhu-library-agent

# 建立並啟動虛擬環境
python -m venv venv
# Windows PowerShell：
.\venv\Scripts\activate
# macOS / Linux：
source venv/bin/activate
```

### 2. 安裝相依套件與 Playwright 瀏覽器核心
```bash
pip install google-genai playwright beautifulsoup4 requests streamlit pillow pydantic tenacity python-dotenv
playwright install chromium
```

### 3. 設定 Gemini API Key
在專案根目錄手動建立 `.env` 檔案，並填入從 [Google AI Studio](https://aistudio.google.com/app/apikey) 申請的金鑰：
```env
GEMINI_API_KEY=你的_AIzaSy_開頭金鑰
```

### 4. 啟動應用程式
```bash
streamlit run app.py
```
啟動後瀏覽器將自動開啟 `http://localhost:8501`。

---

## 🧪 內建 Demo 測試情境

網頁上方提供三個一鍵測試情境，並支援左側欄多模態檔案上傳：
1. **🔍 情境 1（複合條件＋並行檢索）**：搜尋 2020 年後出版、關於「機器學習」或「深度學習」的在架中文書，抓回簡介並規劃拿書路線。
2. **🔄 情境 2（冷門主題自主放寬重試）**：搜尋高限制條件冷門主題，展示 Agent 查無資料後自動放寬條件重搜，以及觸發 3 輪安全煞車的保護機制。
3. **📖 情境 3（多書比較＋閱讀順序建議）**：檢索多本 Python 實戰書籍，比較章節深淺排定閱讀順序，並可一鍵匯出 `.md` 筆記。
4. **📑 多模態課程大綱找書**：於左側欄上傳 `.pdf` 課程大綱或書單截圖，自動辨識書目並查詢東華館藏位置。