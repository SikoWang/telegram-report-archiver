import asyncio
import io
import os
import json
import logging
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

# 支援本地開發載入 .env 檔案
from dotenv import load_dotenv
load_dotenv()

from pydantic import BaseModel, Field
from tenacity import retry, wait_random_exponential, stop_after_attempt, retry_if_exception_type

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
    CommandHandler, # 新增指令處理器支援
)

from google import genai
from google.genai import types
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_DRIVE_PARENT_ID = os.environ.get("GOOGLE_DRIVE_PARENT_ID")

# 全域異步信號量：限制同時下載/解析任務數量為 2，防止容器 OOM
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)

MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
}


class ReportAnalysis(BaseModel):
    category: str = Field(
        description="本報告之最適分類標籤，例如：'財務報告'、'市場分析'、'技術白皮書'、'研究報告'。"
    )
    summary: str = Field(
        description="本報告之繁體中文核心摘要，內容須客觀、結構完整且條理清晰，字數嚴格限制在 250 至 300 字之間。"
    )


def extract_text_from_docx_bytes(docx_bytes: bytes) -> str:
    """
    在記憶體內解開 DOCX ZIP 壓縮包，從 word/document.xml 提取乾淨純文字。
    繞過 Gemini 不支援 DOCX MIME 類型的限制，同時大幅降低 Token 消耗。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes), "r") as docx_zip:
            if "word/document.xml" not in docx_zip.namelist():
                raise ValueError("無效的 DOCX 格式：未檢測到核心 XML 組件。")

            xml_content = docx_zip.read("word/document.xml")
            root = ET.fromstring(xml_content)

            namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            paragraphs = []

            for p_node in root.findall(".//w:p", namespaces):
                t_nodes = p_node.findall(".//w:t", namespaces)
                text_pieces = [t.text for t in t_nodes if t.text]
                if text_pieces:
                    paragraphs.append("".join(text_pieces))

            return "\n\n".join(paragraphs)
    except Exception as e:
        logger.error(f"DOCX XML 降級解析失敗: {e}")
        raise RuntimeError(f"無法解析此 DOCX 文件，異常原因：{e}")


def init_google_drive_service() -> Any:
    """從環境變數載入 Google Drive 憑證，初始化 Google Drive API v3 客戶端。"""
    scopes = ["https://www.googleapis.com/auth/drive"]
    sa_json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json_str:
        sa_info = json.loads(sa_json_str)
        creds = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
        return build("drive", "v3", credentials=creds)

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")

    if not client_id or not client_secret or not refresh_token:
        raise ValueError(
            "環境變數中缺少 Google Drive 驗證配置。請設定 GOOGLE_SERVICE_ACCOUNT_JSON，"
            "或改用 GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / "
            "GOOGLE_OAUTH_REFRESH_TOKEN。"
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


# 延遲載入 Google Drive API 服務，防止啟動時因環境變數缺失而直接崩潰
_drive_service = None


def get_drive_service() -> Any:
    """取得或初始化 Google Drive API 服務 (延遲載入)"""
    global _drive_service
    if _drive_service is None:
        _drive_service = init_google_drive_service()
    return _drive_service


def get_or_create_category_folder(category_name: str, parent_id: str) -> str:
    """在指定根目錄下動態檢索分類資料夾，不存在則自動新建，回傳其 ID。"""
    # 防禦 Drive API 查詢注入：移除名稱中的單引號
    safe_name = category_name.replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and "
        f"mimeType = 'application/vnd.google-apps.folder' and "
        f"'{parent_id}' in parents and "
        f"trashed = false"
    )
    drive = get_drive_service()
    results = drive.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])

    if files:
        folder_id = files[0]["id"]
        logger.info(f"檢索到既存子資料夾: '{category_name}' (ID: {folder_id})")
        return folder_id

    folder_metadata = {
        "name": category_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    new_folder = drive.files().create(body=folder_metadata, fields="id").execute()
    folder_id = new_folder.get("id")
    logger.info(f"動態新建分類子資料夾: '{category_name}' (ID: {folder_id})")
    return folder_id


def upload_and_share_file(
    file_name: str, file_bytes: bytes, mime_type: str, folder_id: str
) -> str:
    """將記憶體字節流上傳至 Google Drive，開啟公開讀取權限，回傳 WebViewLink。"""
    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=True)
    drive = get_drive_service()
    uploaded_file = drive.files().create(
        body=file_metadata, media_body=media, fields="id, webViewLink"
    ).execute()

    file_id = uploaded_file.get("id")
    logger.info(f"檔案上傳成功，ID: {file_id}")

    permission_body = {"role": "reader", "type": "anyone"}
    drive.permissions().create(fileId=file_id, body=permission_body).execute()
    logger.info(f"公開讀取權限設定完成 (ID: {file_id})")

    file_details = drive.files().get(fileId=file_id, fields="webViewLink").execute()
    return file_details.get("webViewLink")


_gemini_client = None


def get_gemini_client() -> genai.Client:
    """取得或初始化 Gemini API 客戶端 (延遲載入)"""
    global _gemini_client
    if _gemini_client is None:
        if not GEMINI_API_KEY:
            raise ValueError("缺少 GEMINI_API_KEY 環境變數，無法初始化 Gemini 客戶端。")
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


@retry(
    wait=wait_random_exponential(min=2, max=60),
    stop=stop_after_attempt(6),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def invoke_gemini_analysis(file_bytes: bytes, mime_type: str, file_name: str) -> ReportAnalysis:
    """
    調用 Gemini 2.5 Pro 執行結構化 JSON 分類與摘要，內置指數退避重試防禦 429。
    """
    system_instruction = (
        "您是一位頂尖的商務與學術文檔分析專家。您的任務是研讀輸入的報告文檔，"
        "為其指定一個最貼切的分類範疇，並撰寫一則繁體中文的摘要。"
        "該摘要必須控制在 250 至 300 字之間，"
        "內容應精確提煉出核心立論、關鍵數據與主要結論。"
    )

    uploaded_file = None
    try:
        client = get_gemini_client()
        if mime_type == "application/pdf":
            logger.info("使用 Files API 上傳 PDF 檔案至 Gemini 以避免 payload 限制...")
            uploaded_file = client.files.upload(
                file=io.BytesIO(file_bytes),
                config=dict(mime_type="application/pdf", display_name=file_name)
            )
            document_part = uploaded_file
        elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            plain_text = extract_text_from_docx_bytes(file_bytes)
            document_part = types.Part.from_text(text=plain_text)
        else:
            try:
                plain_text = file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                plain_text = file_bytes.decode("big5", errors="replace")
            document_part = types.Part.from_text(text=plain_text)

        prompt = f"請對名為 '{file_name}' 的文檔進行全面深度剖析。"

        logger.info("發起 Gemini API 結構化分析請求...")
        gemini_model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
        logger.info(f"使用模型: {gemini_model}")
        response = client.models.generate_content(
            model=gemini_model,
            contents=[document_part, prompt],
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=ReportAnalysis,
                temperature=0.2,
            ),
        )

        parsed_result: ReportAnalysis = response.parsed
        return parsed_result

    finally:
        if uploaded_file is not None:
            try:
                logger.info(f"清理 Gemini 上的臨時檔案: {uploaded_file.name}")
                client.files.delete(name=uploaded_file.name)
            except Exception as delete_err:
                logger.warning(f"清理 Gemini 檔案失敗: {delete_err}")


async def handle_document_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """異步 Telegram 訊息處理核心，含信號量流量整形與完整異常處理。"""
    message = update.message
    if not message or not message.document:
        return

    doc = message.document
    file_name = doc.file_name or "unnamed_file"
    mime_type = doc.mime_type or ""
    file_size = doc.file_size or 0

    if mime_type not in ALLOWED_MIME_TYPES:
        await message.reply_text(
            f"❌ 不支援的檔案類型：`{mime_type}`\n"
            f"目前僅接受 PDF、Word (.docx) 及純文字 (.txt) 報告。",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if file_size > MAX_FILE_SIZE_BYTES:
        size_mb = file_size / (1024 * 1024)
        await message.reply_text(
            f"❌ 檔案大小 {size_mb:.2f} MB 超出 Telegram Bot API 20 MB 下載限制。\n"
            f"請壓縮後重新上傳，或聯繫系統管理員評估本地 API 伺服器方案。"
        )
        return

    status_msg = await message.reply_text("📥 正在從 Telegram 拉取報告字節流...")

    # 信號量確保最多 2 個任務同時執行，防止 OOM
    async with DOWNLOAD_SEMAPHORE:
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            file_bytes = bytes(await tg_file.download_as_bytearray())

            await status_msg.edit_text("🧠 拉取完成，正在以 Gemini 進行深度分析與分類...")

            # Gemini 為同步阻塞調用，使用 run_in_executor 避免阻塞事件迴圈
            loop = asyncio.get_event_loop()
            analysis_result: ReportAnalysis = await loop.run_in_executor(
                None, invoke_gemini_analysis, file_bytes, mime_type, file_name
            )
            category = analysis_result.category
            summary = analysis_result.summary

            await status_msg.edit_text(
                f"🗂️ 分析完成！歸類為：`{category}`\n正在歸檔至 Google Drive...",
                parse_mode=ParseMode.MARKDOWN,
            )

            target_folder_id = await loop.run_in_executor(
                None, get_or_create_category_folder, category, GOOGLE_DRIVE_PARENT_ID
            )
            web_view_link = await loop.run_in_executor(
                None, upload_and_share_file, file_name, file_bytes, mime_type, target_folder_id
            )

            response_text = (
                f"📋 **報告深度分析摘要**\n\n"
                f"📂 **歸檔類別：** `{category}`\n\n"
                f"📝 **智慧摘要（約 300 字）：**\n{summary}\n\n"
                f"🔗 **雲端檔案：** [點此檢視]({web_view_link})"
            )

            await status_msg.delete()
            await message.reply_text(
                response_text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            logger.info(f"報告 '{file_name}' 全管線處理完成。")

        except Exception as e:
            logger.error(f"文檔處理管線異常: {e}", exc_info=True)
            await status_msg.edit_text(
                f"💥 管線中斷，異常原因：\n`{str(e)[:200]}`\n\n請重試或聯繫系統管理員。",
                parse_mode=ParseMode.MARKDOWN,
            )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """處理 /start 指令，提供歡迎詞與使用說明。"""
    user_name = update.effective_user.first_name if update.effective_user else "使用者"
    welcome_text = (
        f"👋 您好，{user_name}！我是**智慧型文件分類歸檔機器人**。\n\n"
        "你可以直接向我發送 PDF、Word (.docx) 或純文字 (.txt) 報告，我將會自動：\n"
        "1. 🧠 使用 Gemini AI 分析報告內容並自動分類。\n"
        "2. 📝 生成大約 300 字的繁體中文核心摘要。\n"
        "3. 📁 自動將報告歸檔至 Google Drive 對應的分類資料夾中。\n"
        "4. 🔗 回傳公開檢視連結與分析摘要給您。\n\n"
        "使用 /help 可取得更多說明。"
    )
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """處理 /help 指令，說明限制與格式。"""
    help_text = (
        "💡 **使用說明與限制**\n\n"
        "📌 **支援格式：**\n"
        "- PDF (`.pdf`)\n"
        "- Word (`.docx`)\n"
        "- 純文字 (`.txt`)\n\n"
        "⚠️ **限制限制：**\n"
        "- 單一檔案大小上限為 **20 MB**（受限於 Telegram Bot API）。\n"
        "- 同時僅能處理 2 個任務，其餘任務會排隊等待。\n\n"
        "🔧 **如何開始：**\n"
        "直接拖曳或上傳報告檔案到此對話，我便會開始處理。"
    )
    if update.message:
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


def main() -> None:
    required_vars = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "GOOGLE_DRIVE_PARENT_ID": GOOGLE_DRIVE_PARENT_ID,
    }
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        logger.critical(f"系統啟動失敗：缺少環境變數 {missing}")
        return

    # 進一步檢查 Google Drive 服務所需的憑證變數
    has_sa = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
    has_oauth = all([
        os.environ.get("GOOGLE_OAUTH_CLIENT_ID"),
        os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"),
        os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    ])
    if not has_sa and not has_oauth:
        logger.critical(
            "系統啟動失敗：缺少 Google Drive 驗證配置。\n"
            "請設定 GOOGLE_SERVICE_ACCOUNT_JSON，或設定完整的 GOOGLE_OAUTH_* 環境變數。"
        )
        return

    logger.info("智慧型文件處理 Telegram 機器人啟動中...")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document_upload))
    application.run_polling()


if __name__ == "__main__":
    main()
