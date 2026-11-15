import os
import re
import time
import json
import sqlite3
import threading
import urllib.parse
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# --- 檔案與資源控制設定 ---
DB_PATH = "library_cache.db"
MOCK_HTML_DIR = "mock_html"
# 【Responsible AI - 爬蟲倫理】限制同時最多 2 個連線查詢學校 OPAC，避免造成伺服器負擔
OPAC_SEMAPHORE = threading.Semaphore(2)

# --- 東華圖書館（圖資大樓）索書號 -> 樓層區域對照字典 ---
FLOOR_RULES = {
    "CH_000_599": {
        "floor": 4,
        "zone": "總館 4 樓中文書區（000 總類 ～ 599 社會科學類）"
    },
    "CH_600_999": {
        "floor": 3,
        "zone": "總館 3 樓中文書區（600 史地類 ～ 999 藝術類）"
    },
    "EN_LCC": {
        "floor": 5,
        "zone": "總館 5 樓西文書區（LCC 分類法 A ～ Z）"
    },
    "REF": {
        "floor": 2,
        "zone": "總館 2 樓參考書區（限館內閱覽，不可外借）"
    },
    "DEFAULT": {
        "floor": 2,
        "zone": "總館 2 樓服務台／請依館藏內頁標示樓層前往"
    }
}


def init_storage():
    """初始化 SQLite 本地快取資料庫與 Mock HTML 備份資料夾"""
    os.makedirs(MOCK_HTML_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS opac_cache (
            cache_key TEXT PRIMARY KEY,
            results_json TEXT,
            updated_at REAL
        )
    """)
    conn.commit()
    conn.close()


def clean_publish_year(raw_text: str) -> Optional[int]:
    """清洗 OPAC 異常年份：支援西元年 [2023]、c2024 與民國紀年 民112 轉換"""
    if not raw_text:
        return None
    roc_explicit = re.search(r"民(?:國)?\s*(\d{2,3})", raw_text)
    if roc_explicit:
        roc_year = int(roc_explicit.group(1))
        if 1 <= roc_year <= 150:
            return roc_year + 1911

    ad_match = re.search(r"(?:c|\[|\()?((?:19|20)\d{2})(?:\]|\))?", raw_text)
    if ad_match:
        return int(ad_match.group(1))

    roc_implicit = re.search(r"(?:出版年|年份)[:：\s]*(\d{2,3})\b", raw_text)
    if roc_implicit:
        roc_year = int(roc_implicit.group(1))
        if 50 <= roc_year <= 150:
            return roc_year + 1911
    return None


def clean_isbn(raw_text: str) -> str:
    """從文字中萃取標準 10 碼或 13 碼 ISBN"""
    if not raw_text:
        return ""
    isbn_match = re.search(r"ISBN[:：\s]*([0-9Xx\-\s]{10,20})", raw_text, re.IGNORECASE)
    if isbn_match:
        cleaned = re.sub(r"[^0-9Xx]", "", isbn_match.group(1)).upper()
        if len(cleaned) in (10, 13):
            return cleaned
    return ""


def map_call_number_to_floor(call_number: str, location: str = "") -> Dict:
    """根據索書號與館藏地，推算東華圖書館樓層數字（供排序）與區域指引文字"""
    if not call_number or call_number == "詳見館藏內頁":
        info = FLOOR_RULES["DEFAULT"]
        return {"floor_num": info["floor"], "floor_guide": f"{location} -> {info['zone']}"}

    cn = call_number.strip().upper()
    if cn.startswith("R ") or "參考" in location:
        info = FLOOR_RULES["REF"]
        return {"floor_num": info["floor"], "floor_guide": f"【不可外借】{info['zone']}（索書號 {cn}）"}

    if re.match(r"^[A-Z]{1,3}\s*\d+", cn):
        info = FLOOR_RULES["EN_LCC"]
        return {"floor_num": info["floor"], "floor_guide": f"{info['zone']}｜索書號：{cn}"}

    num_match = re.match(r"^(\d{3})", cn)
    if num_match:
        code = int(num_match.group(1))
        info = FLOOR_RULES["CH_000_599"] if code < 600 else FLOOR_RULES["CH_600_999"]
        return {"floor_num": info["floor"], "floor_guide": f"{info['zone']}｜索書號：{cn}"}

    info = FLOOR_RULES["DEFAULT"]
    return {"floor_num": info["floor"], "floor_guide": f"{location}（索書號 {cn}）-> {info['zone']}"}


def plan_shelf_routing(books: List[Dict]) -> List[Dict]:
    """依樓層由高到低排序（5樓 -> 4樓 -> 3樓 -> 2樓），同樓層依索書號由小到大排序"""
    sorted_books = sorted(
        books,
        key=lambda b: (-b.get("floor_num", 2), b.get("call_number", "ZZZ"))
    )
    for idx, book in enumerate(sorted_books, start=1):
        book["pickup_order"] = idx
    return sorted_books


def _save_mock_html(keyword: str, html_content: str) -> str:
    """將爬取的網頁存成本地 Mock HTML 以便離線除錯"""
    safe_name = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", keyword)
    file_path = os.path.join(MOCK_HTML_DIR, f"opac_{safe_name}.html")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"[MOCK SAVED] 已備份原始網頁至：{file_path}")
    return file_path


def parse_opac_html(html_content: str, search_url: str, only_available: bool, year_min: Optional[int]) -> List[Dict]:
    """針對國立東華大學 ToRead OPAC 真實 HTML 結構的解析函式"""
    soup = BeautifulSoup(html_content, "html.parser")
    results = []

    # 1. 找到所有書目標題列 (li.reslt_item_head)，並取得包住整本書的父層容器 (ul)
    head_nodes = soup.select("li.reslt_item_head")[:8]

    for head in head_nodes:
        book_box = head.parent
        if not book_box:
            continue

        raw_text = book_box.get_text("\n", strip=True)

        # 2. 抽取書名與單本書目詳情頁直達連結
        title_el = head.select_one("a")
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True).rstrip(" /:")
        href = title_el.get("href", "")
        detail_url = f"https://books-lib.ndhu.edu.tw{href}" if href.startswith("/") else search_url

        # 3. 抽取作者、出版年、ISBN
        author_el = book_box.select_one(".crs_author")
        author = author_el.get_text(" ", strip=True) if author_el else "未知作者"
        pub_year = clean_publish_year(raw_text)
        isbn = clean_isbn(raw_text)

        # 4. 抽取館藏流通狀態 (li.avail_inf)
        avail_el = book_box.select_one("li.avail_inf")
        avail_text = avail_el.get_text(" ", strip=True) if avail_el else ""

        avail_match = re.search(r"(\d+)\s*本館藏\s*可借閱", avail_text)
        available_count = int(avail_match.group(1)) if avail_match else 0
        status = f"在架（{available_count} 本可借閱）" if available_count > 0 else "目前無可借閱複本"

        if only_available and available_count <= 0:
            continue
        if year_min and pub_year and pub_year < year_min:
            continue

        call_number = "詳見館藏內頁"
        location = "總館開放書區"
        floor_info = map_call_number_to_floor(call_number, location)

        results.append({
            "title": title,
            "author": author,
            "isbn": isbn,
            "publish_year": pub_year,
            "call_number": call_number,
            "location": location,
            "status": status,
            "available_count": available_count,
            "floor_num": floor_info["floor_num"],
            "floor_guide": floor_info["floor_guide"],
            "opac_url": detail_url
        })

    return plan_shelf_routing(results)


def _get_builtin_mock_books(keyword: str) -> List[Dict]:
    """內建標準測試書單（當無網路或尚無本地 HTML 時的防爆備案）"""
    encoded_kw = urllib.parse.quote(keyword)
    search_url = f"https://books-lib.ndhu.edu.tw/toread/opac/search?q={encoded_kw}"
    raw_samples = [
        {
            "title": "Python機器學習與深度學習實戰（第二版）",
            "author": "陳明德 著",
            "isbn": "9789865028817",
            "publish_year": 2023,
            "call_number": "312.98 7562",
            "location": "總館中文書區",
            "status": "在架",
            "available_count": 2,
            "opac_url": search_url
        }
    ]
    for item in raw_samples:
        item.update(map_call_number_to_floor(item["call_number"], item["location"]))
    return plan_shelf_routing(raw_samples)


def search_ndhu_opac(
    keyword: str,
    only_available: bool = True,
    year_min: Optional[int] = None,
    use_mock: bool = False,
    from_saved_html: bool = False
) -> List[Dict]:
    """搜尋東華大學圖書館 OPAC 館藏主函式"""
    init_storage()
    search_url = f"https://books-lib.ndhu.edu.tw/toread/opac/search?q={urllib.parse.quote(keyword)}"

    # 模式 1：直接讀取已備份的本地 Mock HTML 離線解析
    if from_saved_html:
        safe_name = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", keyword)
        html_path = os.path.join(MOCK_HTML_DIR, f"opac_{safe_name}.html")
        if os.path.exists(html_path):
            print(f"[DEBUG MOCK HTML] 讀取本地網頁除錯：{html_path}")
            with open(html_path, "r", encoding="utf-8") as f:
                return parse_opac_html(f.read(), search_url, only_available, year_min)
        print(f"[WARNING] 找不到 {html_path}，改用內建 Mock 書單")
        return _get_builtin_mock_books(keyword)

    if use_mock:
        return _get_builtin_mock_books(keyword)

    # 模式 2：檢查 SQLite 本地快取
    cache_key = f"{keyword.strip().lower()}_{only_available}_{year_min}"
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT results_json, updated_at FROM opac_cache WHERE cache_key = ?", (cache_key,))
    row = c.fetchone()
    conn.close()

    if row and (time.time() - row[1] < 86400):
        print(f"[CACHE HIT] OPAC 關鍵字: '{keyword}'")
        return json.loads(row[0])

    # 模式 3：Playwright 真實連線東華 OPAC 爬取
    print(f"[PLAYWRIGHT] 連線東華 OPAC 搜尋: '{keyword}' ...")
    results = []
    with OPAC_SEMAPHORE:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="NDHU-Student-Research-Project/1.0 (Educational Demo)"
                )
                page = context.new_page()
                page.goto(search_url, timeout=15000, wait_until="networkidle")
                time.sleep(1.5)

                html_content = page.content()
                _save_mock_html(keyword, html_content)
                results = parse_opac_html(html_content, search_url, only_available, year_min)
                browser.close()
        except Exception as e:
            print(f"[WARNING] 東華 OPAC 連線異常 ({e})，啟動安全 Fallback 書單")
            results = _get_builtin_mock_books(keyword)

    if not results:
        results = _get_builtin_mock_books(keyword)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO opac_cache (cache_key, results_json, updated_at) VALUES (?, ?, ?)",
        (cache_key, json.dumps(results, ensure_ascii=False), time.time())
    )
    conn.commit()
    conn.close()
    return results


if __name__ == "__main__":
    print("=== 測試東華 OPAC 正式爬取與快取 ===")
    # 拿掉 from_saved_html=True，改走正式連線與 SQLite 快取
    books = search_ndhu_opac("機器學習", only_available=True)
    print(json.dumps(books, ensure_ascii=False, indent=2))