import gspread
from google.oauth2.service_account import Credentials
from pytz import timezone
from datetime import datetime

# ====================================================
# 🟩 Helper: Google Sheets Setup (Global)
# ====================================================

GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
GOOGLE_CREDS_FILE = "firebase_key.json"
SHEET_ID = "1TB76T6A1oWFi4T0iXdl2jfeGP1dC2MFSU-ESB3cBnVg"
CLOSED_TRADES_FILE = "closed_trades.csv"

def get_google_sheet():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=GOOGLE_SCOPE)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open("Closed Trades Journal").worksheet("journal")
    return sheet

def test_append_row():
    sheet = get_google_sheet()
    now_nz = datetime.now(timezone("Pacific/Auckland"))
    day_date = now_nz.strftime("%A %d %B %Y")

    test_row = [
        day_date,              # 1. day_date
        now_nz.isoformat(),    # 2. entry_exit_time
        1,                     # 3. number_of_contracts
        "TEST_TRADE_TYPE",     # 4. trade_type
        "No",                  # 5. fifo_match
        123.45,                # 6. entry_price
        130.50,                # 7. exit_price
        10,                    # 8. trail_trigger_value
        5,                     # 9. trail_offset
        140.00,                # 10. trailing_take_profit
        4.0,                   # 11. trail_offset_amount
        "N/A",                 # 12. ema_flatten_type
        "N/A",                 # 13. ema_flatten_triggered
        1.25,                  # 14. spread
        200.50,                # 15. net_pnl
        7.02,                  # 16. tiger_commissions
        193.48,                # 17. realized_pnl
        "TEST_TRADE_ID",       # 18. trade_id
        "MATCHED_TRADE_ID",    # 19. fifo_match_order_id
        "TEST_SOURCE",         # 20. source
        "No notes"             # 21. manual_notes
    ]

    sheet.append_row(test_row)
    print("✅ Test row appended successfully!")

if __name__ == "__main__":
    test_append_row()