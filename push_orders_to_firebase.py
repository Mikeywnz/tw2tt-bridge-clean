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
    
    # === Setup Tiger API ===
    config = TigerOpenClientConfig()
    client = TradeClient(config)
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

    # Prune stale open trades to keep Firebase clean
    prune_stale_open_trades()

    # === Prune stale /open_trades/ entries not backed by active Tiger orders ===
def prune_stale_open_trades():
    try:
        open_trades_ref = db.reference("/open_trades/MGC2508")
        tiger_orders_ref = db.reference("/tiger_orders")
        pruned_log_ref = db.reference("/pruned_log")

        open_trades_snapshot = open_trades_ref.get() or {}
        tiger_orders_snapshot = tiger_orders_ref.get() or {}

        # Build set of active order_ids from tiger_orders with status indicating open or filled but not cancelled
        active_order_ids = set()
        for key, order in tiger_orders_snapshot.items():
            order_id = order.get("order_id")
            status = order.get("status", "").upper()
            # Consider these statuses as active orders (adjust as needed)
            if order_id and status in {"FILLED", "OPEN", "PARTIALLY_FILLED"}:
                active_order_ids.add(order_id)

        deleted_count = 0

        for trade_id, trade_data in open_trades_snapshot.items():
            trade_order_id = trade_data.get("order_id", "")
            if trade_order_id not in active_order_ids:
                print(f"🧹 Pruning stale open_trade: {trade_id} with order_id {trade_order_id}")

                # Delete from open_trades
                open_trades_ref.child(trade_id).delete()
                deleted_count += 1

                # Mark in pruned_log
                pruned_log_ref.child(trade_id).set(True)

                # Optional: log to Google Sheets if desired
                # Add your Google Sheets logging function call here if available

        print(f"✅ Pruned {deleted_count} stale open_trades entries.")
    except Exception as e:
        print(f"❌ Failed to prune stale open_trades: {e}")

    # === No Position Flattening: Auto-close if no Tiger positions exist ===
    try:
        positions = client.get_positions(account="21807597867063647", sec_type=SegmentType.FUT)
        if len(positions) == 0:
            print("⚠️ No TigerTrade positions detected. Checking open_trades...")

            open_trades_ref = db.reference("/open_trades")
            trades = open_trades_ref.get() or {}

            now = datetime.utcnow()
            for key, trade in trades.items():
                # Skip if already closed or missing fields
                if not isinstance(trade, dict):
                    continue

                entry_time = trade.get("entry_timestamp")
                if not entry_time:
                    continue

                print(f"🛑 Flattening ghost trade: {key}")
                # Push to Google Sheets (assumes log_to_google_sheets() already exists)
                log_to_google_sheets({
                    "symbol": trade.get("symbol", ""),
                    "action": trade.get("action", ""),
                    "entry_price": safe_float(trade.get("entry_price")),
                    "exit_price": 0.0,
                    "pnl_dollars": 0.0,
                    "reason_for_exit": "no_position_detected",
                    "entry_time": entry_time,
                    "exit_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "trail_triggered": trade.get("trail_hit", False)
                })

                # Remove from Firebase
                open_trades_ref.child(key).delete()
                print(f"✅ Removed ghost trade: {key}")

        # Cleanup: remove any stale symbols no longer in Tiger positions
        firebase_snapshot = db.reference("/live_positions").get() or {}
        seen_symbols = {str(getattr(pos, "contract", "")).split("/")[0] for pos in positions}
        for symbol in firebase_snapshot:
            if symbol not in seen_symbols:
                print(f"🧹 Deleting stale /live_positions/{symbol}")
                db.reference("/live_positions").child(symbol).delete()

    except Exception as e:
        print(f"🔥 ERROR during no-position flattening: {e}")

# === SAFE ENTRY POINT ===
if __name__ == "__main__":
    push_orders_main()