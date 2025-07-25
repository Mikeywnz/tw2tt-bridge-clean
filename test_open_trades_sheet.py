import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2.service_account import Credentials as NewCredentials
from datetime import datetime
import pytz

# --- Shared configs ---
GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
GOOGLE_CREDS_FILE = "firebase_key.json"
SHEET_NAME = "Closed Trades Journal"  # Spreadsheet name

# --- Test Open Trades sheet ---
def test_open_trades_sheet_append():
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, GOOGLE_SCOPE)
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).worksheet("Open Trades Journal")

        now_nz = datetime.now(pytz.timezone("Pacific/Auckland"))
        day_date = now_nz.strftime("%A %d %B %Y")

        test_row = [
            day_date,
            "TEST_SYMBOL",
            "BUY",
            "LONG_ENTRY",
            1234.56,
            14.0,
            5.0,
            round(1234.56 + 14.0, 2),
            "TEST_ORDER_ID",
            now_nz.isoformat()
        ]

        sheet.append_row(test_row)
        print("✅ Successfully appended test row to Open Trades Google Sheet.")
    except Exception as e:
        print(f"❌ Failed to append to Open Trades Google Sheet: {e}")

# --- Test Closed Trades sheet ---
def test_closed_trades_sheet_append():
    try:
        creds = NewCredentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=GOOGLE_SCOPE)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(client.open(SHEET_NAME).id).worksheet("journal")

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
        print("✅ Successfully appended test row to Closed Trades Google Sheet.")
    except Exception as e:
        print(f"❌ Failed to append to Closed Trades Google Sheet: {e}")

if __name__ == "__main__":
    test_open_trades_sheet_append()
    test_closed_trades_sheet_append()