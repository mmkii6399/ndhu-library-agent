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

DB_PATH = "library_cache.db"
MOCK_HTML_DIR = "mock_html"
OPAC_SEMAPHORE = threading.Semaphore(2)

# --- 國立東華大學圖書館（圖資大樓 A 棟）官方樓層與書區對照字典 ---
FLOOR_RULES = {
    "EN_HC_Z": {
        "floor": 6,
        "zone": "總館 6 樓西文書區（HC–Z）"
    },
    "EN_A_HB": {
        "floor": 5,
        "zone": "總館 5 樓西文書區（A–HB）"
    },
    "THESIS_5F": {
        "floor": 5,
        "zone": "總館 5 樓博碩士論文區／中文套書區"
    },
    "CH_000_599": {
        "floor": 4,
        "zone": "總館 4 樓中文書區（000–599）"
    },
    "CH_600_999": {
        "floor": 3,
        "zone": "總館 3 樓中文書區（600–999）"
    },
    "REF_2F": {
        "floor": 2,
        "zone": "總館 2 樓中／西文參考書區（限館內閱覽，不可外借）"
    },
    "SPECIAL_2F": {
        "floor": 2,
        "zone": "總館 2 樓名家藏書／東華精粹／期刊視聽區"
    },
    "SPECIAL_1F": {
        "floor": 1,
        "zone": "總館 1 樓楊牧書房／兒童圖書區／現刊區"
    },
    "DEFAULT": {
        "floor": 1,
        "zone": "總館 1 樓服務檯（請依館藏內頁標示樓層前往）"
    }
}


def init_storage():
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
    if not raw_text:
        return None
    roc_explicit = re.search(r"民(?:國)?\s*(\d{2,3})", raw_text)
    if roc_explicit:
        roc_year = int(roc_explicit.group(1))
        if 1 <= roc_year <= 150:
            return roc_year + 1911

    # 優先找「出版年: 2020」避免誤抓作者生卒年 (1931-)
    pub_match = re.search(r"出版年[:：\s]*(?:c|\[|\()?((?:19|20)\d{2})", raw_text)
    if pub_match:
        return int(pub_match.group(1))

    ad_match = re.search(r"(?:c|\[|\()?((?:19|20)\d{2})(?:\]|\))?", raw_text)
    if ad_match:
        return int(ad_match.group(1))
    return None


def clean_isbn(raw_text: str) -> str:
    if not raw_text:
        return ""
    isbn_match = re.search(r"ISBN[:：\s]*([0-9Xx\-\s]{10,20})", raw_text, re.IGNORECASE)
    if isbn_match:
        cleaned = re.sub(r"[^0-9Xx]", "", isbn_match.group(1)).upper()
        if len(cleaned) in (10, 13):
            return cleaned
    return ""


def map_call_number_to_floor(call_number: str, location: str = "") -> Dict:
    """根據東華圖書館官方樓層配置，優先依館藏地字串、其次依索書號精準推算樓層"""
    cn = (call_number or "").strip().upper()
    loc = (location or "").strip()
    loc_upper = loc.upper()

    # 1. 優先比對詳情頁抓到的真實「館藏地」字串
    if "六樓" in loc or "6F" in loc_upper or "HC-Z" in loc_upper:
        return {"floor_num": 6, "floor_guide": f"{loc}｜索書號：{cn}"}
    if "五樓" in loc or "5F" in loc_upper or "A-HB" in loc_upper or "論文" in loc or "套書" in loc:
        return {"floor_num": 5, "floor_guide": f"{loc}｜索書號：{cn}"}
    if "四樓" in loc or "4F" in loc_upper or "000-599" in loc or "愛的書庫" in loc:
        return {"floor_num": 4, "floor_guide": f"{loc}｜索書號：{cn}"}
    if "三樓" in loc or "3F" in loc_upper or "600-999" in loc or "大尺寸" in loc:
        return {"floor_num": 3, "floor_guide": f"{loc}｜索書號：{cn}"}
    if "二樓" in loc or "2F" in loc_upper or any(k in loc for k in ["參考書", "名家", "精粹", "視聽", "裝訂本"]):
        return {"floor_num": 2, "floor_guide": f"{loc}｜索書號：{cn}"}
    if "一樓" in loc or "1F" in loc_upper or any(k in loc for k in ["楊牧", "兒童", "現刊"]):
        return {"floor_num": 1, "floor_guide": f"{loc}｜索書號：{cn}"}

    # 2. 若僅有索書號，依東華官方分類區間推算
    if not cn or cn == "詳見館藏內頁":
        info = FLOOR_RULES["DEFAULT"]
        return {"floor_num": info["floor"], "floor_guide": f"{loc} -> {info['zone']}"}

    if cn.startswith("R "):
        info = FLOOR_RULES["REF_2F"]
        return {"floor_num": info["floor"], "floor_guide": f"【不可外借】{info['zone']}（索書號 {cn}）"}

    lcc_match = re.match(r"^([A-Z]{1,3})\s*\d+", cn)
    if lcc_match:
        prefix = lcc_match.group(1)
        if prefix[0] < "H" or prefix in ("H", "HA", "HB"):
            info = FLOOR_RULES["EN_A_HB"]
        else:
            info = FLOOR_RULES["EN_HC_Z"]
        return {"floor_num": info["floor"], "floor_guide": f"{loc or info['zone']}｜索書號：{cn}"}

    num_match = re.match(r"^(\d{3})", cn)
    if num_match:
        code = int(num_match.group(1))
        info = FLOOR_RULES["CH_000_599"] if code < 600 else FLOOR_RULES["CH_600_999"]
        return {"floor_num": info["floor"], "floor_guide": f"{loc or info['zone']}｜索書號：{cn}"}

    info = FLOOR_RULES["DEFAULT"]
    return {"floor_num": info["floor"], "floor_guide": f"{loc}（索書號 {cn}）"}


def plan_shelf_routing(books: List[Dict]) -> List[Dict]:
    sorted_books = sorted(
        books,
        key=lambda b: (-b.get("floor_num", 1), b.get("call_number", "ZZZ"))
    )
    for idx, book in enumerate(sorted_books, start=1):
        book["pickup_order"] = idx
    return sorted_books


def _save_mock_html(keyword: str, html_content: str) -> str:
    safe_name = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", keyword)
    file_path = os.path.join(MOCK_HTML_DIR, f"opac_{safe_name}.html")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"[MOCK SAVED] 已備份原始網頁至：{file_path}", flush=True)
    return file_path


def _enrich_holdings_from_detail(page, book: Dict) -> None:
    """進入單本書目詳情頁 (detail_url)，抓出真實館藏地（如六樓西文書區HC-Z）與索書號"""
    detail_url = book.get("opac_url", "")
    if "bibliographic_view" not in detail_url:
        return

    try:
        print(f"[DETAIL FETCH] 進入內頁查詢館藏位置：{book['title'][:30]}...", flush=True)
        page.goto(detail_url, timeout=12000, wait_until="networkidle")
        time.sleep(0.8)
        detail_html = page.content()
        soup = BeautifulSoup(detail_html, "html.parser")

        # 搜尋詳情頁內所有表格列，找包含館藏地關鍵字（樓／書區／在架）的資料列
        for tr in soup.select("table tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.select("td") if td.get_text(strip=True)]
            if not cells:
                continue
            row_text = " | ".join(cells)

            # 判斷是否為館藏複本資料列（排除表頭「館藏地」）
            if "館藏地" in row_text or "條碼號" in row_text:
                continue

            if any(k in row_text for k in ["樓", "書區", "在架", "Western", "Books"]):
                loc_found = next(
                    (c for c in cells if any(w in c for w in ["樓", "書區", "專區", "論文", "F "])),
                    ""
                )
                # 尋找索書號（西文 LCC 如 TK5101 .H37 2001 或中文分類號 如 312.98 7562）
                # 尋找索書號（明確排除東華館藏條碼號 W + 一串數字，如 W0845123）
                cn_found = ""
                for c in cells:
                    c_clean = c.strip()
                    if c_clean == loc_found:
                        continue
                    # 【關鍵過濾】跳過條碼號（W 開頭接純數字）與純數字欄位
                    if re.match(r"^W\d+$", c_clean, re.IGNORECASE) or c_clean.isdigit():
                        continue
                    # 匹配真正的西文 LCC 索書號（如 TK5101 .H37 2001）或中文索書號（如 312.98 7562）
                    m = re.search(
                        r"\b([A-Z]{1,3}\s*\d+(?:\.\d+)?(?:\s+[A-Z0-9\.\-]+)+|\d{3}(?:\.\d+)?\s+[\d\-]+)\b",
                        c_clean
                    )
                    if m:
                        cn_found = m.group(1).strip()
                        break

                if loc_found or cn_found:
                    if loc_found:
                        book["location"] = loc_found
                    if cn_found:
                        book["call_number"] = cn_found
                    book.update(map_call_number_to_floor(book["call_number"], book["location"]))
                    print(f"  -> 成功取得：{book['location']}｜索書號：{book['call_number']}（{book['floor_num']}樓）", flush=True)
                    return

        # 若表格未解析到，備份該詳情頁供檢查 DOM
        with open(os.path.join(MOCK_HTML_DIR, "debug_detail_page.html"), "w", encoding="utf-8") as f:
            f.write(detail_html)
        print("  -> [INFO] 詳情頁表格未匹配到欄位，已備份至 mock_html/debug_detail_page.html", flush=True)

    except Exception as e:
        print(f"[WARNING] 詳情頁館藏解析略過: {e}", flush=True)


def parse_opac_html(html_content: str, search_url: str, only_available: bool, year_min: Optional[int]) -> List[Dict]:
    soup = BeautifulSoup(html_content, "html.parser")
    results = []
    head_nodes = soup.select("li.reslt_item_head")[:8]

    for head in head_nodes:
        book_box = head.parent
        if not book_box:
            continue

        raw_text = book_box.get_text("\n", strip=True)
        title_el = head.select_one("a")
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True).rstrip(" /:")
        href = title_el.get("href", "")
        detail_url = f"https://books-lib.ndhu.edu.tw{href}" if href.startswith("/") else search_url

        author_el = book_box.select_one(".crs_author")
        author = author_el.get_text(" ", strip=True) if author_el else "未知作者"
        pub_year = clean_publish_year(raw_text)
        isbn = clean_isbn(raw_text)

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


def search_ndhu_opac(
    keyword: str,
    only_available: bool = True,
    year_min: Optional[int] = None,
    use_mock: bool = False,
    from_saved_html: bool = False
) -> List[Dict]:
    init_storage()
    search_url = f"https://books-lib.ndhu.edu.tw/toread/opac/search?q={urllib.parse.quote(keyword)}"

    cache_key = f"{keyword.strip().lower()}_{only_available}_{year_min}"
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT results_json, updated_at FROM opac_cache WHERE cache_key = ?", (cache_key,))
    row = c.fetchone()
    conn.close()

    if row and (time.time() - row[1] < 86400):
        print(f"[CACHE HIT] OPAC 關鍵字: '{keyword}'", flush=True)
        return json.loads(row[0])

    print(f"[PLAYWRIGHT] 連線東華 OPAC 搜尋: '{keyword}' ...", flush=True)
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

                # 進入前 3 本可借閱書籍的詳情頁抓出真實館藏地（如六樓西文書區HC-Z）與索書號
                for book in results[:3]:
                    _enrich_holdings_from_detail(page, book)

                results = plan_shelf_routing(results)
                browser.close()
        except Exception as e:
            print(f"[WARNING] 東華 OPAC 連線異常 ({e})", flush=True)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO opac_cache (cache_key, results_json, updated_at) VALUES (?, ?, ?)",
        (cache_key, json.dumps(results, ensure_ascii=False), time.time())
    )
    conn.commit()
    conn.close()
    return results