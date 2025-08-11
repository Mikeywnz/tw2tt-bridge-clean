import gspread
from google.oauth2.service_account import Credentials
from pytz import timezone
from datetime import datetime

GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
GOOGLE_CREDS_FILE = "firebase_key.json"
SHEET_ID = "1TB76T6A1oWFi4T0iXdl2jfeGP1dC2MFSU-ESB3cBnVg"
CLOSED_TRADES_FILE = "closed_trades.csv"

def get_google_sheet():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=GOOGLE_SCOPE)
    gs_client = gspread.authorize(creds)
    # Opening the "Closed Trades Journal" spreadsheet with worksheet "journal"
    sheet = gs_client.open("Closed Trades Journal").worksheet("journal")
    return sheet

def safe_float(val):
    try:
        return float(val)
    except:
        return 0.0

def map_source(raw_source):
    if raw_source is None:
        return "unknown"
    lower = raw_source.lower()
    if "openapi" in lower:
        return "OpGo"
    elif "desktop" in lower:
        return "Tiger Desktop"
    elif "mobile" in lower:
        return "tiger-mobile"
    elif "liquidation" in lower:
        return "Tiger Liquidation"
    return "unknown"

def test_append_row():
    sheet = get_google_sheet()
    now_nz = datetime.now(timezone("Pacific/Auckland"))
    day_date = now_nz.strftime("%A %d %B %Y")

    symbol_for_log = "TEST_SYMBOL"
    action = "BUY"
    trade_type = "LONG"
    trade_id = "TEST_TRADE_ID"
    entry_timestamp = now_nz.isoformat()
    source = "test-source"

    trade_data = {
        "entry_price": 123.45,
        "trail_trigger_value": 10,
        "trail_offset": 5,
        "trailing_take_profit": 140.00,
        "fifo_match": "No",
        "fifo_match_order_id": "N/A",
        "exit_price": "N/A",
        "ema_flatten_type": "N/A",
        "ema_flatten_triggered": "N/A",
        "spread": "N/A",
        "net_pnl": "N/A",
        "tiger_commissions": "N/A",
        "realized_pnl": "N/A",
        "manual_notes": ""
    }

    sheet.append_row([
        day_date,                 # 1. day_date
        "TEST_SYMBOL",            # 2. symbol
        "BUY",                   # 3. action
        "LONG",                  # 4. trade_type (Short/Long)
        123.45,                  # 5. entry_price
        10,                      # 6. trail_trigger (pts)
        5,                       # 7. trail_offset (pts)
        140.00,                  # 8. trailing_take_profit price
        "TEST_TRADE_ID",         # 9. tiger_order_id
        day_date + "T12:00:00Z", # 10. entry_time (UTC)
        "OpGo",                  # 11. source
        "No",                    # 12. fifo_match (Yes/No)
        "N/A",                   # 13. fifo_match_order_id
        "N/A",                   # 14. exit_price (N/A for open trades)
        "N/A",                   # 15. ema_flatten_type
        "N/A",                   # 16. ema_flatten_triggered
        1.25,                    # 17. spread
        200.50,                  # 18. net_pnl
        7.02,                    # 19. tiger_commissions
        193.48,                  # 20. realized_pnl
        "No notes"               # 21. manual_notes
    ])
    
    print("✅ Open trade test row appended successfully!")

if __name__ == "__main__":
    test_append_row()