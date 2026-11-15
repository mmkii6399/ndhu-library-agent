import os
import re
import time
import json
import sqlite3
import urllib.parse
import requests
from bs4 import BeautifulSoup
from typing import List, Dict, Optional, Any
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential
from google import genai
from google.genai import types
from PIL import Image

# 對接成員 A 的東華 OPAC 爬蟲模組
from opac_crawler import search_ndhu_opac, init_storage, DB_PATH

load_dotenv()

# 【Responsible AI - 安全護欄】限制單次對話最多執行 3 輪 Tool Calling，防止無窮迴圈
MAX_TOOL_TURNS = 3

# 【Responsible AI - 系統高可用性】優先使用低延遲高速模型，遇擁擠或超時自動無縫切換
MODEL_CANDIDATES = [
    "gemini-3.5-flash",
    "gemini-3.8-flash",
    "gemini-flash-latest",
]


def init_summary_db():
    """初始化外部書籍摘要的 SQLite 本地快取表（與成員 A 共用 library_cache.db）"""
    init_storage()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS summary_cache (
            book_key TEXT PRIMARY KEY,
            summary TEXT,
            source_type TEXT,
            updated_at REAL
        )
    """)
    conn.commit()
    conn.close()


def _save_summary_cache(book_key: str, summary: str, source_type: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO summary_cache (book_key, summary, source_type, updated_at) VALUES (?, ?, ?, ?)",
        (book_key, summary, source_type, time.time())
    )
    conn.commit()
    conn.close()


def _search_books_com_tw(query_str: str) -> Optional[str]:
    """副備援：從博客來公開搜尋頁抓取繁體中文書籍簡介摘要"""
    try:
        encoded_q = urllib.parse.quote(query_str)
        url = f"https://search.books.com.tw/search/query/key/{encoded_q}/cat/all"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            item = soup.select_one(".table-td, .box_1, .item")
            if item:
                desc_el = item.select_one("p, .txt_box")
                if desc_el:
                    clean_desc = re.sub(r"\s+", " ", desc_el.get_text(" ", strip=True))
                    if len(clean_desc) > 25:
                        return clean_desc[:350]
    except Exception as e:
        print(f"[WARNING] 博客來備援查詢略過: {e}", flush=True)
    return None


def fetch_book_summary(isbn: str = "", title: str = "") -> Dict[str, str]:
    """
    查詢書籍外部內容簡介（SQLite 快取 -> Google Books API 主 -> 博客來 副 -> 誠實邊界標記）。
    """
    init_summary_db()
    book_key = isbn.strip() if isbn.strip() else title.strip()
    if not book_key:
        return {
            "summary": "無足夠書目識別資訊，請依館藏分類推測主題。",
            "source_type": "inferred_from_subject"
        }

    # 1. 檢查 SQLite 本地快取（7 天內有效）
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT summary, source_type, updated_at FROM summary_cache WHERE book_key = ?", (book_key,))
    row = c.fetchone()
    conn.close()
    if row and (time.time() - row[2] < 604800):
        print(f"[CACHE HIT] 書籍摘要: '{book_key}' ({row[1]})", flush=True)
        return {"summary": row[0], "source_type": row[1]}

    # 2. 主來源：查詢 Google Books API
    query = f"isbn:{isbn.strip()}" if isbn.strip() else f"intitle:{title.strip()}"
    try:
        resp = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": query, "maxResults": 1},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("totalItems", 0) > 0:
                vol_info = data["items"][0].get("volumeInfo", {})
                desc = vol_info.get("description", "")
                categories = ", ".join(vol_info.get("categories", []))
                if desc:
                    prefix = f"【分類：{categories}】" if categories else ""
                    summary_text = f"{prefix}{desc[:400]}"
                    _save_summary_cache(book_key, summary_text, "google_books_api")
                    return {"summary": summary_text, "source_type": "google_books_api"}
    except Exception as e:
        print(f"[WARNING] Google Books API 查詢異常: {e}", flush=True)

    # 3. 副來源：博客來搜尋頁備援（專補中文書）
    time.sleep(0.4)
    books_tw_desc = _search_books_com_tw(book_key)
    if books_tw_desc:
        _save_summary_cache(book_key, books_tw_desc, "books_com_tw")
        return {"summary": books_tw_desc, "source_type": "books_com_tw"}

    # 4. 【Responsible AI - 誠實邊界】均無外部簡介時，明確回傳推估標記，嚴禁模型編造目錄
    fallback_msg = "此書在外部公開資料庫查無詳細目錄與簡介，請依書名與館藏分類推估主題。"
    _save_summary_cache(book_key, fallback_msg, "inferred_from_subject")
    return {"summary": fallback_msg, "source_type": "inferred_from_subject"}


SYSTEM_INSTRUCTION = """
你是一位「國立東華大學圖書館 AI 館員助理」，負責協助學生查詢館藏書籍並規劃最省力的拿書路線。
請嚴格遵守以下【Responsible AI 與作業規範】：

1. **關鍵字拆解與自主重試**：
   - 東華 OPAC 為字面檢索，收到自然語言需求（或課程大綱截圖）時，請拆解為最多 2 個精簡核心關鍵字並行呼叫 `tool_search_opac`。
   - 若第一次搜尋結果為空，請自動放寬條件（移除年份限制或改用更廣泛的上位詞）重試一次；若仍無結果，請停止搜尋並建議使用「全國文獻傳遞服務系統（NDDS）」。

2. **絕對防幻覺與誠實邊界原則**：
   - 推薦書籍的「書名、作者、出版年、索書號、館藏地、在架狀態、OPAC 連結」必須 **100% 來自工具回傳的真實資料**，嚴禁自行編造！
   - 針對篩選出的前 3 本最佳書籍，請並行呼叫 `tool_fetch_summary` 取得內容簡介。
   - **重要警語規則**：若 `tool_fetch_summary` 回傳的 `source_type` 為 `inferred_from_subject`，你**必須**在該書介紹旁明確標註：「⚠️ 此書無外部公開目錄，以下簡介為 AI 依據書名與館藏主題推估，借閱前請以實體書為準」。

3. **隱私與資安邊界**：
   - 絕對不要求、不收集使用者的東華單一簽入（SSO）學號或密碼，僅提供 OPAC 直達連結供使用者自行登入預約。

4. **結構化輸出格式**：
   - **最佳推薦書單**：列出書名、作者、出版年、索書號／館藏指引、內容簡介與推薦理由，並附上 `[查看東華 OPAC 館藏](opac_url)` 連結。
   - **最省力拿書路徑規劃**：依據工具回傳的 `pickup_order` 與 `floor_guide`（由高樓層到低樓層），整理不走回頭路的取書順序清單。
   - **建議閱讀順序**：比較各書深淺，建議先讀哪本建立基礎、再讀哪本進階。
"""


class LibraryAgent:
    def __init__(self, use_mock_opac: bool = False):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("找不到 GEMINI_API_KEY，請確認專案目錄下已建立 .env 檔案！")
        # 設定 25 秒 HTTP 超時（25000 ms），絕不讓單次請求卡死數分鐘
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=25000)
        )
        self.use_mock_opac = use_mock_opac

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=3))
    def _call_gemini(self, contents: List[Any], config: types.GenerateContentConfig):
        """依序嘗試候選模型，遇額度上限、超時或伺服器擁擠時自動無縫切換（不重複浪費配額）"""
        last_err = None
        for model_name in MODEL_CANDIDATES:
            try:
                return self.client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config
                )
            except Exception as e:
                last_err = e
                err_msg = str(e)
                print(f"[FALLBACK] 模型 {model_name} 略過 ({type(e).__name__}: {err_msg[:60]})，切換下一個備援模型...", flush=True)
                time.sleep(1)
                continue
        raise last_err

    def _build_config(self, tools=None) -> types.GenerateContentConfig:
      """建立極速低延遲推論設定（關閉冗長思考鏈 + 低溫度防幻覺）"""
      kwargs: Dict[str, Any] = {
          "system_instruction": SYSTEM_INSTRUCTION,
          "temperature": 0.1,
      }
      if tools:
          kwargs["tools"] = tools
          kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
      # 嘗試將思考預算壓至最低，加快回應速度
      try:
          kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
      except Exception:
          pass
      return types.GenerateContentConfig(**kwargs)

    def run(
        self,
        user_query: str,
        image: Optional[Image.Image] = None,
        pdf_bytes: Optional[bytes] = None,
        status_callback=None
    ) -> Dict[str, Any]:
        """
        執行 Agent 主迴圈，支援純文字、圖片截圖與 PDF 課程大綱檔案輸入。
        """
        def log_step(msg: str):
            print(f"[AGENT STEP] {msg}", flush=True)
            if status_callback:
                status_callback(msg)

        def tool_search_opac(keyword: str, only_available: bool = True, year_min: Optional[int] = None) -> List[Dict]:
            """搜尋東華大學圖書館 OPAC 館藏。keyword 應為精簡核心關鍵字。"""
            books = search_ndhu_opac(
                keyword=keyword,
                only_available=only_available,
                year_min=year_min,
                use_mock=self.use_mock_opac
            )
            return books[:3]

        def tool_fetch_summary(isbn: str = "", title: str = "") -> Dict[str, str]:
            """根據 ISBN 或書名抓取書籍外部內容簡介。"""
            return fetch_book_summary(isbn=isbn, title=title)

        config = self._build_config(tools=[tool_search_opac, tool_fetch_summary])

        # 組合多模態輸入 Parts（支援文字 + 圖片 或 PDF 課程大綱）
        user_parts: List[Any] = [types.Part.from_text(text=user_query)]
        if image is not None:
            user_parts.append(image)
            log_step("📸 偵測到上傳圖片，啟動多模態課程大綱／書單辨識...")
        if pdf_bytes is not None:
            user_parts.append(
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
            )
            log_step("📄 偵測到上傳 PDF 課程大綱，啟動文件解析與參考書目萃取...")

        conversation: List[types.Content] = [
            types.Content(role="user", parts=user_parts)
        ]

        trace_logs: List[Dict[str, Any]] = []
        turn_count = 0

        while turn_count < MAX_TOOL_TURNS:
            turn_count += 1
            t0 = time.time()
            log_step(f"🧠 第 {turn_count}/{MAX_TOOL_TURNS} 輪思考：Gemini 正在分析需求與決策...")
            response = self._call_gemini(conversation, config)
            log_step(f"⚡ 第 {turn_count} 輪模型決策完成（耗時 {time.time() - t0:.1f} 秒）")

            candidate = response.candidates[0]
            conversation.append(candidate.content)

            function_calls = [
                part.function_call for part in candidate.content.parts if part.function_call
            ]

            if not function_calls:
                log_step("✅ 資訊整合完畢，正在生成結構化書單與拿書路線！")
                return {
                    "answer": response.text,
                    "trace_logs": trace_logs,
                    "turns_used": turn_count
                }

            tool_response_parts = []
            for fc in function_calls:
                fn_name = fc.name
                fn_args = dict(fc.args)
                log_step(f"🔧 調用工具 `{fn_name}` | 參數: {json.dumps(fn_args, ensure_ascii=False)}")

                if fn_name == "tool_search_opac":
                    fn_result = tool_search_opac(**fn_args)
                elif fn_name == "tool_fetch_summary":
                    fn_result = tool_fetch_summary(**fn_args)
                else:
                    fn_result = {"error": f"未知工具: {fn_name}"}

                trace_logs.append({
                    "turn": turn_count,
                    "tool": fn_name,
                    "args": fn_args,
                    "result_count": len(fn_result) if isinstance(fn_result, list) else 1
                })

                tool_response_parts.append(
                    types.Part.from_function_response(
                        name=fn_name,
                        response={"result": fn_result}
                    )
                )

            conversation.append(types.Content(role="user", parts=tool_response_parts))
            time.sleep(0.8)

        # 達到 MAX_TOOL_TURNS 上限時強制收斂（安全煞車）
        log_step("⚠️ 已達工具調用上限（3 輪），啟動安全護欄進行最終彙整...")
        conversation.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="已達搜尋次數上限，請立即根據目前已取得的館藏資料為使用者整理最終推薦結果。")]
            )
        )
        final_config = self._build_config(tools=None)
        final_resp = self._call_gemini(conversation, final_config)
        return {
            "answer": final_resp.text,
            "trace_logs": trace_logs,
            "turns_used": turn_count
        }


if __name__ == "__main__":
    print("=== 啟動 LibraryAgent 端到端整合測試 ===")
    t_start = time.time()
    agent = LibraryAgent(use_mock_opac=False)
    res = agent.run("我想找 2020 年以後出版、關於「機器學習」或「深度學習」的在架中文書，請幫我抓取簡介並規劃拿書路線")

    print(f"\n=== 總耗時：{time.time() - t_start:.1f} 秒 ===")
    print("=== Agent 思考與工具調用軌跡 ===")
    print(json.dumps(res["trace_logs"], ensure_ascii=False, indent=2))
    print("\n=== 最終生成推薦書單 ===")
    print(res["answer"])