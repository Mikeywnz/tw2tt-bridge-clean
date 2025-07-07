import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === STEP 1: Auth ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("service_account.json", scope)
client = gspread.authorize(creds)

# === STEP 2: Open Google Sheet ===
sheet = client.open("Closed Trades Journal").sheet1  # This accesses the first sheet tab

# === STEP 3: Append dummy trade data ===
row = ["MGC2508", "SELL", 2375.3, 2374.0, 0.55, "2025-07-06 22:15"]
sheet.append_row(row)

print("✅ Dummy trade successfully logged to Google Sheet.")