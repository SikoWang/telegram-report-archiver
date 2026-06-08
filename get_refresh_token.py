import sys
import os

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("❌ 請先安裝 google-auth-oauthlib 套件以執行此指令：")
    print("   pip install google-auth-oauthlib")
    sys.exit(1)

# 設定 Google Drive API 的權限範圍 (與 main.py 一致)
SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    client_secret_file = "client_secrets.json"

    if not os.path.exists(client_secret_file):
        print(f"❌ 找不到 '{client_secret_file}' 檔案！")
        print("💡 請先前往 Google Cloud Console 建立 OAuth 用戶端 ID (應用程式類型選擇「桌上型應用程式」)，")
        print("   下載該用戶端金鑰 JSON 檔案，並將其重新命名為 'client_secrets.json' 放置於此目錄下。")
        sys.exit(1)

    print("🔑 開始進行 Google OAuth 授權流程...")
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
    # 本地啟動伺服器接收授權 callback
    creds = flow.run_local_server(port=0)

    print("\n🎉 授權成功！請複製以下變數，填入您的 .env 檔案或 Railway 的環境變數中：\n")
    print("=" * 60)
    print(f"GOOGLE_OAUTH_CLIENT_ID={creds.client_id}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={creds.client_secret}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 60)


if __name__ == "__main__":
    main()
