from google.oauth2.service_account import Credentials
import gspread
from datetime import datetime
from pytz import timezone

GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
GOOGLE_CREDS_FILE = "firebase_key.json"
SHEET_NAME = "Closed Trades Journal"
WORKSHEET_NAME = "journal"

def get_google_sheet():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=GOOGLE_SCOPE)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open(SHEET_NAME).worksheet(WORKSHEET_NAME)
    return sheet

def test_append_row():
    sheet = get_google_sheet()
    now_nz = datetime.now(timezone("Pacific/Auckland"))
    test_row = [
        now_nz.strftime("%A %d %B %Y"),  # Date
        "TEST_SYMBOL",
        "TEST_STATUS",
        "TEST_ACTION",
        "TEST_TYPE",
        123.45,  # Entry price
        10.0,    # Exit price placeholder
        100.0,   # PnL placeholder
        "TEST_REASON",
        now_nz.strftime("%Y-%m-%d %H:%M:%S"),  # Entry time
        now_nz.strftime("%Y-%m-%d %H:%M:%S"),  # Exit time
        False,
        "TEST_TRADE_ID",
        "TEST_EXIT_ORDER_ID",
        "TEST_EXIT_METHOD"
    ]
    sheet.append_row(test_row)
    print("✅ Test row appended successfully!")

if __name__ == "__main__":
    test_append_row()