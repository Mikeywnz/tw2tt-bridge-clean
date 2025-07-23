#=========================  PUSH_ORDERS_TO_FIREBASE - PART 1  ================================
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
import requests
import json

FIREBASE_URL = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"

# === Load Trailing Take Profit Settings from Firebase ===
def load_trailing_tp_settings():
    try:
        fb_url = f"{FIREBASE_URL}/trailing_tp_settings.json"
        res = requests.get(fb_url)
        cfg = res.json() if res.ok else {}
        if cfg.get("enabled", False):
            return float(cfg.get("trigger_points", 14.0)), float(cfg.get("offset_points", 5.0))
    except Exception as e:
        print(f"⚠️ Failed to fetch trailing TP settings: {e}")
    return 14.0, 5.0
    
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

#=====  END OF PART 1 =====

#=========================  PUSH_ORDERS_TO_FIREBASE - PART 2  ================================

# === MAIN FUNCTION WRAPPED HERE ===
def push_orders_main():

    tiger_orders_ref = db.reference("/tiger_orders_log")
    open_trades_ref = db.reference("open_active_trades")

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
        try:
            oid = str(getattr(order, 'id', '')).strip()
            if not oid:
                print("⚠️ Skipping order with empty or missing ID")
                continue

            tiger_ids.add(oid)

            # Extract order info
            status = str(getattr(order, "status", "")).split('.')[-1].upper()
            reason = str(getattr(order, "reason", "")).split('.')[-1] if getattr(order, "reason", "") else ""
            filled = getattr(order, "filled", 0)
            exit_reason_raw = get_exit_reason(status, reason, filled)

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

            # Parse symbol
            raw_contract = str(getattr(order, 'contract', ''))
            symbol = raw_contract.split('/')[0] if '/' in raw_contract else raw_contract

            # Construct payload
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

            # ✅ Always push raw Tiger order into tiger_orders_log
            tiger_orders_ref.child(oid).set(payload)
            # === STEP 2 FIFI EXIT MATCHING LOGIC: If this is a closing trade, match it to an open trade ===
            action = payload.get("action")
            symbol = payload.get("symbol")
            order_time = payload.get("order_time")
            opposite_action = "SELL" if action == "BUY" else "BUY"

            # Get current open trades for this symbol
            open_trades = open_trades_ref.child(symbol).get() or {}

            # Find the oldest opposite trade (FIFO-style)
            matched_trade_id = None
            for tid, trade in sorted(open_trades.items(), key=lambda item: item[1].get("entry_timestamp", 0)):
                if trade.get("action") == opposite_action and trade.get("trade_state", "open") == "open":
                    matched_trade_id = tid
                    break

            # If we found a match, mark it as closed and remove it
            if matched_trade_id:
                print(f"🟡 Matched closing trade: {action} {symbol} → closes {opposite_action} {matched_trade_id}")

                # Mark it as closed in Firebase
                open_trades_ref.child(symbol).child(matched_trade_id).update({
                    "trade_state": "closed",
                    "exit_reason": "FIFO Close",
                    "exit_time": order_time
                })

                # Remove from /open_trades/
                open_trades_ref.child(symbol).child(matched_trade_id).delete()
                print(f"✅ Removed from /open_trades/: {symbol}/{matched_trade_id}")
         
            print(f"✅ Pushed to Firebase Tiger Orders Log: {oid}")

            # === Only push to /open_trades/ if still open ===
            if payload.get("is_open", False) and payload.get("status") == "FILLED" and payload.get("filled", 0) > 0:
                price = payload["avg_fill_price"]
                action = payload["action"]
                entry_timestamp = getattr(order, "order_time", None)
                trigger_points, offset_points = load_trailing_tp_settings()

                endpoint = f"{FIREBASE_URL}/open_trades/{symbol}.json"
                resp = requests.get(endpoint)
                existing = resp.json() or {}
                trade_id = oid

                new_trade = {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "entry_price": price,
                    "action": action,
                    "contracts_remaining": 1,
                    "trail_trigger": trigger_points,
                    "trail_offset": offset_points,
                    "trail_hit": False,
                    "trail_peak": price,
                    "filled": True,
                    "entry_timestamp": entry_timestamp,
                    "exit_reason": "",
                    "trade_state": "open"
                }

                existing[trade_id] = new_trade
                put = requests.put(endpoint, json=existing)

                if put.status_code == 200:
                    print(f"✅ /open_trades/{symbol}/{trade_id} successfully updated")
                else:
                    print(f"❌ Failed to update /open_trades/{symbol}/{trade_id}: {put.text}")

        except Exception as e:
            print(f"❌ Firebase push failed for {oid}: {e}")

    # === Tally Summary ===
    print(f"✅ FILLED: {filled_count}")
    print(f"❌ CANCELLED: {cancelled_count}")
    print(f"🚫 LACK_OF_MARGIN: {lack_margin_count}")
    print(f"🟡 UNKNOWN: {unknown_count}")

#=====  END OF PART 2 =====
 
#=========================  PUSH_ORDERS_TO_FIREBASE - PART 3 (FINAL PART)  ================================

def handle_position_flattening():
    try:
        positions = client.get_positions(account="21807597867063647", sec_type=SegmentType.FUT)
        live_ref = db.reference("/live_positions")
        open_trades_ref = db.reference("/open_active_trades")

        # ✅ Push live Tiger positions into Firebase
        print("📊 Open Positions:")
        for pos in positions:
            contract = str(getattr(pos, "contract", ""))
            quantity = getattr(pos, "quantity", 0)
            avg_cost = getattr(pos, "average_cost", 0.0)
            market_price = getattr(pos, "market_price", 0.0)

            print(f"contract: {contract}, quantity: {quantity}, average_cost: {avg_cost}, market_price: {market_price}")
            symbol = contract.split("/")[0] if "/" in contract else contract

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

        # ✅ Check for open trades that are no longer matched by Tiger positions
        tiger_symbols = {str(getattr(pos, "contract", "")).split("/")[0] for pos in positions}
        all_open_trades = open_trades_ref.get() or {}

        for symbol, trades_by_id in all_open_trades.items():
            if symbol in tiger_symbols:
                continue  # Tiger still shows this position — skip

            for trade_id, trade in trades_by_id.items():
                if not isinstance(trade, dict):
                    continue

                now_nz = datetime.now(timezone("Pacific/Auckland"))
                print(f"🛑 Flattening manually closed trade: {symbol} / {trade_id}")

                day_date = now_nz.strftime("%A %d %B %Y")
                exit_time_str = now_nz.strftime("%Y-%m-%d %H:%M:%S")

                exit_price = 0.0  # Optional placeholder for now
                pnl_dollars = 0.0
                exit_reason = "manual_flattened"
                exit_order_id = "MANUAL"
                fifo_close = False

                # ✅ Log to Google Sheets
                sheet.append_row([
                    day_date,
                    trade.get("symbol", ""),
                    "closed",                            # NEW: status field
                    trade.get("action", ""),
                    safe_float(trade.get("entry_price")),
                    exit_price,                          # From Tiger or 0.0 if unknown
                    pnl_dollars,                         # Optional: placeholder 0.0 for now
                    exit_reason,                         # e.g. "trailing_tp", "manual_flattened"
                    trade.get("entry_timestamp", ""),
                    exit_time_str,                       # e.g. datetime string
                    trade.get("trail_hit", False),
                    trade.get("trade_id", ""),
                    exit_order_id,                       # Real ID or "MANUAL"
                    fifo_close                           # True or False
                ])

                # ✅ Mark trade as closed instead of deleting
                open_trades_ref.child(symbol).child(trade_id).update({
                    "trade_state": "closed",
                    "exit_reason": "manual_flattened",
                    "exit_time": now_nz.strftime("%Y-%m-%d %H:%M:%S")
                })

        # ✅ Cleanup: remove stale live_positions not seen in Tiger positions
        try:
            firebase_snapshot = live_ref.get() or {}
            for symbol in firebase_snapshot:
                if symbol not in tiger_symbols:
                    print(f"🧹 Deleting stale /live_positions/{symbol}")
                    live_ref.child(symbol).delete()
        except Exception as e:
            print(f"❌ Failed to clean stale live_positions: {e}")

    except Exception as e:
        print(f"🔥 ERROR during manual flattening check: {e}")

# === SAFE ENTRY POINT ===
if __name__ == "__main__":
    push_orders_main()
    handle_position_flattening()

#=====  END OF PART 3 (END OF SCRIPT) =====