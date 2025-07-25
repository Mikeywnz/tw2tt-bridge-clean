import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import pytz

# --- Configuration ---
GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
GOOGLE_CREDS_FILE = "firebase_key.json"
SHEET_ID = "1TB76T6A1oWFi4T0iXdl2jfeGP1dC2MFSU-ESB3cBnVg"
WORKSHEET_NAME = "journal"  # Change if testing Open Trades sheet

def test_google_sheets_append():
    try:
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=GOOGLE_SCOPE)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)

        now_nz = datetime.now(pytz.timezone("Pacific/Auckland"))
        day_date = now_nz.strftime("%A %d %B %Y")
        time_str = now_nz.strftime("%H:%M:%S")

        test_row = [
            day_date,
            "TEST_SYMBOL",
            "closed",
            "BUY",
            "LONG_ENTRY",
            1234.56,         # entry_price
            0.0,             # exit_price placeholder
            0.0,             # pnl_dollars placeholder
            "test_log",
            now_nz.isoformat(),
            time_str,
            False,           # trail_triggered
            "TEST_TRADE_ID",
            "TEST_EXIT_ORDER_ID",
            "test_exit_method"
        ]

        sheet.append_row(test_row)
        print("✅ Successfully appended test row to Google Sheet.")
    except Exception as e:
        print(f"❌ Failed to append to Google Sheet: {e}")

if __name__ == "__main__":
    test_google_sheets_append()