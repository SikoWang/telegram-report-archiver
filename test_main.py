import unittest
import os
import io
import zipfile
from unittest.mock import patch, MagicMock

# 我們不希望在導入 main 時真的去連接 API
# 由於 main.py 內部在頂部調用了 load_dotenv，並在 main() 時才檢查環境變數，所以直接導入是安全的！
import main


class TestTelegramReportArchiver(unittest.TestCase):

    def test_extract_text_from_docx_bytes(self):
        """測試 docx 位元組資料的 XML 解析與文字擷取是否正常。"""
        # 建立一個記憶體內的 mock docx ZIP 壓縮檔
        docx_io = io.BytesIO()
        with zipfile.ZipFile(docx_io, "w") as docx_zip:
            # 寫入簡單的 word/document.xml 結構
            document_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
                '  <w:body>\n'
                '    <w:p>\n'
                '      <w:t>第一段文字內容。</w:t>\n'
                '    </w:p>\n'
                '    <w:p>\n'
                '      <w:t>第二段關鍵數據：12345。</w:t>\n'
                '    </w:p>\n'
                '  </w:body>\n'
                '</w:document>'
            )
            docx_zip.writestr("word/document.xml", document_xml)

        docx_bytes = docx_io.getvalue()
        extracted_text = main.extract_text_from_docx_bytes(docx_bytes)

        self.assertIn("第一段文字內容。", extracted_text)
        self.assertIn("第二段關鍵數據：12345。", extracted_text)
        self.assertEqual(extracted_text, "第一段文字內容。\n\n第二段關鍵數據：12345。")

    @patch("main.logger")
    def test_main_missing_env_vars(self, mock_logger):
        """測試缺少基本環境變數時，main() 是否能優雅輸出 critical 錯誤日誌。"""
        # 清空環境變數，以測試缺少環境變數的情況
        with patch.dict(os.environ, {}, clear=True):
            # 重新加載 main.py 的模組級變數值（此時變數為 None）
            with patch("main.TELEGRAM_TOKEN", None), \
                 patch("main.GEMINI_API_KEY", None), \
                 patch("main.GOOGLE_DRIVE_PARENT_ID", None):
                
                main.main()
                mock_logger.critical.assert_called()
                # 檢查是否含有 "缺少環境變數" 的字眼
                args, kwargs = mock_logger.critical.call_args
                self.assertTrue(any("缺少環境變數" in arg for arg in args))

    @patch("main.logger")
    def test_main_missing_drive_configs(self, mock_logger):
        """測試基本變數齊全但缺少 Google Drive 憑證時，main() 是否能優雅地阻擋啟動。"""
        fake_env = {
            "TELEGRAM_TOKEN": "12345:FakeToken",
            "GEMINI_API_KEY": "AIzaFakeKey",
            "GOOGLE_DRIVE_PARENT_ID": "FakeParentFolderId",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            with patch("main.TELEGRAM_TOKEN", fake_env["TELEGRAM_TOKEN"]), \
                 patch("main.GEMINI_API_KEY", fake_env["GEMINI_API_KEY"]), \
                 patch("main.GOOGLE_DRIVE_PARENT_ID", fake_env["GOOGLE_DRIVE_PARENT_ID"]):
                
                main.main()
                mock_logger.critical.assert_called()
                args, kwargs = mock_logger.critical.call_args
                self.assertTrue(any("缺少 Google Drive 驗證配置" in arg for arg in args))
    def test_report_analysis_schema(self):
        """測試 ReportAnalysis 模型是否包含 category、summary 和 title 欄位。"""
        analysis = main.ReportAnalysis(
            category="測試分類",
            summary="這是一個測試摘要，內容大約有三字。",
            title="測試標題"
        )
        self.assertEqual(analysis.category, "測試分類")
        self.assertEqual(analysis.summary, "這是一個測試摘要，內容大約有三字。")
        self.assertEqual(analysis.title, "測試標題")


if __name__ == "__main__":
    unittest.main()
