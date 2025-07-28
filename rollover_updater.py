import firebase_admin
from firebase_admin import credentials, db
from datetime import datetime, timedelta
import pytz
import calendar

# Initialize Firebase
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
})

# Helpers
def get_active_contract_suffix():
    ref = db.reference("/active_contract/MGC")
    symbol = ref.get()
    if not symbol or len(symbol) < 7:
        return None
    return symbol[-4:]  # e.g. "2510"

def set_active_contract(symbol):
    ref = db.reference("/active_contract")
    ref.update({"MGC": symbol})
    print(f"✅ Updated active contract to {symbol}")

def third_friday(year, month):
    # Find third Friday of month/year in NZ timezone
    cal = calendar.Calendar(firstweekday=calendar.MONDAY)
    monthcal = cal.monthdatescalendar(year, month)
    fridays = [day for week in monthcal for day in week if day.weekday() == calendar.FRIDAY and day.month == month]
    return fridays[2]  # third Friday (0-based)

def next_contract_suffix(current_suffix):
    year = 2000 + int(current_suffix[:2])
    month = int(current_suffix[2:])
    month += 2
    if month > 12:
        month -= 12
        year += 1
    return f"{str(year)[2:]}{month:02d}"

def main():
    nz_tz = pytz.timezone("Pacific/Auckland")
    now_nz = datetime.now(nz_tz).date()

    current_suffix = get_active_contract_suffix()
    if not current_suffix:
        print("❌ Could not find valid current contract suffix in Firebase.")
        return

    year = 2000 + int(current_suffix[:2])
    month = int(current_suffix[2:])
    expiry_date = third_friday(year, month)

    print(f"Current contract suffix: {current_suffix}, expiry date: {expiry_date}, today: {now_nz}")

    if now_nz >= expiry_date:
        next_suffix = next_contract_suffix(current_suffix)
        new_symbol = "MGC" + next_suffix
        set_active_contract(new_symbol)
    else:
        print("No rollover needed today.")

if __name__ == "__main__":
    main()