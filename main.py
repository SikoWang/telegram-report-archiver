import asyncio
import io
import os
import json
import logging
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

from pydantic import BaseModel, Field
from tenacity import retry, wait_random_exponential, stop_after_attempt, retry_if_exception_type

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

from google import genai
from google.genai import types
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
    """從環境變數安全載入服務帳戶憑證，初始化 Google Drive API v3 客戶端。"""
    sa_json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json_str:
        raise ValueError("環境變數中缺少 GOOGLE_SERVICE_ACCOUNT_JSON 配置。")

    sa_info = json.loads(sa_json_str)
    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
    return build("drive", "v3", credentials=creds)


drive_service = init_google_drive_service()


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
    results = drive_service.files().list(q=query, fields="files(id)").execute()
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
    new_folder = drive_service.files().create(body=folder_metadata, fields="id").execute()
    folder_id = new_folder.get("id")
    logger.info(f"動態新建分類子資料夾: '{category_name}' (ID: {folder_id})")
    return folder_id


def upload_and_share_file(
    file_name: str, file_bytes: bytes, mime_type: str, folder_id: str
) -> str:
    """將記憶體字節流上傳至 Google Drive，開啟公開讀取權限，回傳 WebViewLink。"""
    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=True)
    uploaded_file = drive_service.files().create(
        body=file_metadata, media_body=media, fields="id, webViewLink"
    ).execute()

    file_id = uploaded_file.get("id")
    logger.info(f"檔案上傳成功，ID: {file_id}")

    permission_body = {"role": "reader", "type": "anyone"}
    drive_service.permissions().create(fileId=file_id, body=permission_body).execute()
    logger.info(f"公開讀取權限設定完成 (ID: {file_id})")

    file_details = drive_service.files().get(fileId=file_id, fields="webViewLink").execute()
    return file_details.get("webViewLink")


gemini_client = genai.Client(api_key=GEMINI_API_KEY)


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

    if mime_type == "application/pdf":
        document_part = types.Part.from_bytes(data=file_bytes, mime_type="application/pdf")
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
    response = gemini_client.models.generate_content(
        model="gemini-2.5-pro",
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

    logger.info("智慧型文件處理 Telegram 機器人啟動中...")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document_upload))
    application.run_polling()


if __name__ == "__main__":
    main()
