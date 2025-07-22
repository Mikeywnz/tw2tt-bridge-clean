from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType, OrderStatus  # ✅ correct on Render!
import firebase_admin
from firebase_admin import credentials, db
import random
import string
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import csv
from pytz import timezone
    
# === Setup Tiger API ===
config = TigerOpenClientConfig()
client = TradeClient(config)

# === Firebase Init ===
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/'
})

logged_ghost_ids_ref = db.reference("/logged_ghost_ids")
logged_ghost_order_ids = set(logged_ghost_ids_ref.get() or [])

# === Google Sheets Setup (Global) ===
from google.oauth2.service_account import Credentials

GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
GOOGLE_CREDS_FILE = "firebase_key.json"
SHEET_ID = "1TB76T6A1oWFi4T0iXdl2jfeGP1dC2MFSU-ESB3cBnVg"
CLOSED_TRADES_FILE = "closed_trades.csv"

creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=GOOGLE_SCOPE)
gs_client = gspread.authorize(creds)
sheet = gs_client.open_by_key(SHEET_ID).worksheet("journal")

# === Manual Flatten Block Helper ===
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
    return "unknown"

def get_exit_reason(status, reason, filled):
    if status == "FILLED":
        return "FILLED"
    elif status == "CANCELLED" and filled == 0:
        return "CANCELLED"
    elif status == "EXPIRED" and filled == 0 and reason and ("资金" in reason or "margin" in reason.lower()):
        return "LACK_OF_MARGIN"
    elif "liquidation" in reason.lower():
        return "liquidation"
    return status

# === MAIN FUNCTION WRAPPED HERE ===
def push_orders_main():

    tiger_orders_ref = db.reference("/tiger_orders")

    # Initialize counters here, BEFORE the order loop:
    filled_count = 0
    cancelled_count = 0
    lack_margin_count = 0
    unknown_count = 0

    REASON_MAP = {
        "trailing_tp_exit": "Trailing Take Profit",
        "manual_close": "Manual Close",
        "ema_flattening_exit": "EMA Flattening",
        "liquidation": "Liquidation",
        "LACK_OF_MARGIN": "Lack of Margin",
        "FILLED": "FILLED",
        "CANCELLED": "Cancelled",
        "EXPIRED": "Lack of Margin",
        # Add raw Tiger strings mapped here if needed
    }

    # === Time Window: last 48 hours ===
    now = datetime.utcnow()
    start_time = now - timedelta(hours=48)
    end_time = now

    orders = client.get_orders(
        account="21807597867063647",
        seg_type=SegmentType.FUT,
        start_time=start_time.strftime("%Y-%m-%d %H:%M:%S"),
        end_time=end_time.strftime("%Y-%m-%d %H:%M:%S"),
        limit=150
    )

    print(f"\n📦 Total orders returned: {len(orders)}")

    tiger_ids = set()
    for order in orders:
        oid = str(getattr(order, 'id', '')).strip()
        if not oid:
            continue
        tiger_ids.add(oid)

        status = str(getattr(order, "status", "")).split('.')[-1].upper()
        reason = str(getattr(order, "reason", "")).split('.')[-1] if getattr(order, "reason", "") else ""
        filled = getattr(order, "filled", 0)
        exit_reason_raw = get_exit_reason(status, reason, filled)

        # Tally increments here:
        if exit_reason_raw == "FILLED":
            filled_count += 1
        elif exit_reason_raw == "CANCELLED":
            cancelled_count += 1
        elif exit_reason_raw == "LACK_OF_MARGIN":
            lack_margin_count += 1
        else:
            unknown_count += 1

        # Normalize timestamp
        raw_ts = getattr(order, 'order_time', 0)
        try:
            ts_int = int(raw_ts)
            if ts_int > 1e12:
                ts_int //= 1000
            iso_ts = datetime.utcfromtimestamp(ts_int).isoformat() + 'Z'
        except Exception:
            iso_ts = None

        # Detect ghost
        ghost_statuses = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
        is_ghost = filled == 0 and status in ghost_statuses

        # Map friendly reason
        friendly_reason = REASON_MAP.get(exit_reason_raw, exit_reason_raw)

        raw_contract = str(getattr(order, 'contract', ''))
        symbol = raw_contract.split('/')[0] if '/' in raw_contract else raw_contract

        payload = {
            "order_id": oid,
            "symbol": symbol,
            "action": str(getattr(order, 'action', '')).upper(),
            "quantity": getattr(order, 'quantity', 0),
            "filled": filled,
            "avg_fill_price": getattr(order, 'avg_fill_price', 0.0),
            "status": status,
            "reason": friendly_reason,
            "liquidation": getattr(order, 'liquidation', False),
            "timestamp": iso_ts,
            "source": map_source(getattr(order, 'source', None)),
            "is_open": getattr(order, 'is_open', False),
            "is_ghost": is_ghost,
            "exit_reason": friendly_reason
        }

        try:
            tiger_orders_ref.child(oid).set(payload)
            print(f"✅ Pushed to Firebase: {oid}")
        except Exception as e:
            print(f"❌ Firebase push failed for {oid}: {e}")

    # === Tally Summary ===
    print(f"✅ FILLED: {filled_count}")
    print(f"❌ CANCELLED: {cancelled_count}")
    print(f"🚫 LACK_OF_MARGIN: {lack_margin_count}")
    print(f"🟡 UNKNOWN: {unknown_count}")

# === Prune stale /open_trades/ entries - logging only, no deletion ===
# prune_stale_open_trades()  # <-- keep this call commented out in main

def prune_stale_open_trades():
    try:
        open_trades_ref = db.reference("/open_trades/MGC2508")

        open_trades_snapshot = open_trades_ref.get() or {}

        for trade_id, trade_data in open_trades_snapshot.items():
            # Just log to Google Sheets for auditing, no deletion
            try:
                now_nz = datetime.now(timezone("Pacific/Auckland"))
                day_date = now_nz.strftime("%A %d %B %Y")  # e.g., "Monday 21 July 2025"

                row = {
                    "day_date": day_date,
                    "symbol": trade_data.get("symbol", ""),
                    "direction": trade_data.get("action", ""),
                    "entry_price": safe_float(trade_data.get("entry_price")),
                    "exit_price": 0.0,
                    "pnl_dollars": 0.0,
                    "reason_for_exit": "pruned_stale_open_trade (LOG ONLY)",
                    "entry_time": trade_data.get("entry_timestamp", ""),
                    "exit_time": now_nz.strftime("%Y-%m-%d %H:%M:%S"),
                    "trail_triggered": trade_data.get("trail_hit", False),
                    "order_id": trade_data.get("order_id", "")
                }

                sheet.append_row(list(row.values()))
                print(f"✅ Logged stale trade to Google Sheets: {trade_id}")

            except Exception as e:
                print(f"❌ Failed to log stale trade {trade_id} to Google Sheets: {e}")

        print(f"✅ Logged {len(open_trades_snapshot)} open_trades entries to Google Sheets (no deletion).")

    except Exception as e:
        print(f"❌ Failed in prune_stale_open_trades logging: {e}")

   
# === Flattening function ===  # === No Position Flattening: Auto-close if no Tiger positions exist ===
def handle_position_flattening():
    try:
        positions = client.get_positions(account="21807597867063647", sec_type=SegmentType.FUT)

        print("📊 Open Positions:")
        for pos in positions:
            contract = getattr(pos, "contract", "")
            quantity = getattr(pos, "quantity", 0)
            avg_cost = getattr(pos, "average_cost", 0.0)
            market_price = getattr(pos, "market_price", 0.0)
            print(f"contract: {contract}, quantity: {quantity}, average_cost: {avg_cost}, market_price: {market_price}")

            contract_str = str(contract)
            symbol = contract_str.split("/")[0] if "/" in contract_str else contract_str

            # Push live position to Firebase
            try:
                live_ref.child(symbol).set({
                    "quantity": quantity,
                    "average_cost": avg_cost,
                    "market_price": market_price,
                    "timestamp": datetime.utcnow().isoformat()
                })
                print(f"✅ Updated live_positions for {symbol}")
    except Exception as e:
        print(f"❌ Failed to update live_positions for {symbol}: {e}")

        if len(positions) == 0:
            print("⚠️ No TigerTrade positions detected. Checking open_trades...")

            open_trades_ref = db.reference("/open_trades")
            trades = open_trades_ref.get() or {}

            now_nz = datetime.now(timezone("Pacific/Auckland"))
            for key, trade in trades.items():
                if not isinstance(trade, dict):
                    continue
                entry_time = trade.get("entry_timestamp")
                if not entry_time:
                    continue

                print(f"🛑 Flattening ghost trade: {key}")
                # Push to Google Sheets
                day_date = now_nz.strftime("%A %d %B %Y")
                log_to_google_sheets({
                    "day_date": day_date,
                    "symbol": trade.get("symbol", ""),
                    "action": trade.get("action", ""),
                    "entry_price": safe_float(trade.get("entry_price")),
                    "exit_price": 0.0,
                    "pnl_dollars": 0.0,
                    "reason_for_exit": "no_position_detected",
                    "entry_time": entry_time,
                    "exit_time": now_nz.strftime("%Y-%m-%d %H:%M:%S"),
                    "trail_triggered": trade.get("trail_hit", False),
                    "order_id": trade.get("order_id", "")
                })

                # Remove from Firebase
                open_trades_ref.child(key).delete()
                print(f"✅ Removed ghost trade: {key}")

        # Optional: clean stale live_positions if needed (comment if unsure)
        # firebase_snapshot = db.reference("/live_positions").get() or {}
        # seen_symbols = {str(getattr(pos, "contract", "")).split("/")[0] for pos in positions}
        # for symbol in firebase_snapshot:
        #     if symbol not in seen_symbols:
        #         print(f"🧹 Deleting stale /live_positions/{symbol}")
        #         db.reference("/live_positions").child(symbol).delete()

    except Exception as e:
        print(f"🔥 ERROR during no-position flattening: {e}")

# === SAFE ENTRY POINT ===
if __name__ == "__main__":
    push_orders_main()
    handle_position_flattening()