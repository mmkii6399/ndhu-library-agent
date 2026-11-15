import json
import streamlit as st
from PIL import Image
from agent_core import LibraryAgent, MAX_TOOL_TURNS

st.set_page_config(
    page_title="東華圖書館 AI 館員助理",
    page_icon="📚",
    layout="wide"
)

# --- 側邊欄：Responsible AI 透明化聲明與多模態上傳 ---
with st.sidebar:
    st.header("🛡️ Responsible AI 安全與設計規範")
    st.info(
        "**1. 透明度與可解釋性**\n"
        "即時公開 Agent 拆解的關鍵字、工具調用軌跡與東華 OPAC 原始直達連結。\n\n"
        "**2. 防幻覺與誠實邊界**\n"
        "推論溫度設為 `0.1`；若冷門書查無外部簡介，系統將明確顯示 AI 推估警語，絕不捏造目錄。\n\n"
        "**3. 隱私與資安邊界**\n"
        "絕不索取或儲存東華 SSO 學號密碼，預約一律導向官方頁面自行登入。\n\n"
        "**4. 資源節制與爬蟲倫理**\n"
        f"單次對話上限 `{MAX_TOOL_TURNS}` 輪防無窮迴圈，搭配 SQLite 本地快取減少學校伺服器負擔[cite: 3, 4]。"
    )

    st.divider()
    st.subheader("📑 多模態課程大綱／書單找書")
    uploaded_file = st.file_uploader(
        "上傳課程大綱（PDF 或截圖）或參考書單",
        type=["pdf", "png", "jpg", "jpeg"]
    )
    uploaded_image = None
    uploaded_pdf_bytes = None

    if uploaded_file:
        if uploaded_file.type == "application/pdf" or uploaded_file.name.lower().endswith(".pdf"):
            uploaded_pdf_bytes = uploaded_file.getvalue()
            st.success(f"📄 已載入 PDF 課綱：{uploaded_file.name}（{len(uploaded_pdf_bytes) // 1024} KB）")
        else:
            uploaded_image = Image.open(uploaded_file)
            st.image(uploaded_image, caption="已載入課程大綱圖片", use_container_width=True)

# --- 主畫面標題與一鍵 Demo 情境按鈕 ---
st.title("📚 國立東華大學圖書館 AI 館員助理")
st.caption("結合 Gemini Function Calling、東華 OPAC 即時館藏檢索與跨樓層拿書路徑規劃[cite: 2]")

st.markdown("**💡 快速測試情境（點擊直接執行）：**")
col1, col2, col3 = st.columns(3)
preset_query = None

if col1.button("🔍 情境 1：複合條件找書與路線", use_container_width=True):
    preset_query = "我想找 2020 年以後出版、關於「機器學習」或「深度學習」的在架中文書，請幫我抓取簡介並規劃拿書路線"

if col2.button("🔄 情境 2：冷門主題自主放寬重試", use_container_width=True):
    preset_query = "幫我找 2025 年以後東華圖書館關於「量子拓撲量子計算超導晶片」的在架書籍，找不到請自動放寬條件重試"

if col3.button("📖 情境 3：Python 實戰與閱讀順序", use_container_width=True):
    preset_query = "想自學 Python 資料分析與機器學習，請幫我找目前在架的中文書，推薦閱讀順序並排拿書路線"

# 初始化對話紀錄與 Agent 實例
if "messages" not in st.session_state:
    st.session_state.messages = []
if "agent" not in st.session_state:
    st.session_state.agent = LibraryAgent(use_mock_opac=False)

# 渲染歷史對話
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("trace_logs"):
            with st.expander("🔎 查看 Agent 決策與工具調用軌跡（JSON）"):
                st.json(msg["trace_logs"])

# 接收使用者輸入或情境按鈕指令
user_input = st.chat_input("請輸入想找的書籍主題、年份限制，或搭配左側上傳課程大綱截圖...")
active_query = preset_query if preset_query else user_input

if active_query:
    st.session_state.messages.append({"role": "user", "content": active_query})
    with st.chat_message("user"):
        st.markdown(active_query)

    with st.chat_message("assistant"):
        with st.status("🤖 AI 館員正在執行自主檢索與規劃...", expanded=True) as status_box:
            def update_ui_status(step_msg: str):
                st.write(step_msg)

            result = st.session_state.agent.run(
                user_query=active_query,
                image=uploaded_image,
                pdf_bytes=uploaded_pdf_bytes,
                status_callback=update_ui_status
            )
            status_box.update(
                label=f"✅ 檢索與規劃完成（共執行 {result['turns_used']}/{MAX_TOOL_TURNS} 輪決策）",
                state="complete",
                expanded=False
            )

        st.markdown(result["answer"])

        with st.expander("🔎 查看 Agent 決策與工具調用軌跡（JSON）"):
            st.json(result["trace_logs"])

        st.download_button(
            label="📥 匯出本次推薦書單與拿書路線 (.md)",
            data=result["answer"],
            file_name="ndhu_library_reading_plan.md",
            mime="text/markdown"
        )

    st.session_state.messages.append({
        "role": "assistant",
        "content": result["answer"],
        "trace_logs": result["trace_logs"]
    })