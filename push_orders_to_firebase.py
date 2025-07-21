from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType
import firebase_admin
from firebase_admin import credentials, db
import random
import string
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === Firebase Init ===
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/'
})

# === Google Sheets Setup (Global) ===
from google.oauth2.service_account import Credentials

GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
GOOGLE_CREDS_FILE = "firebase_key.json"
SHEET_ID = "1TB76T6A1oWFi4T0iXdl2jfeGP1dC2MFSU-ESB3cBnVg"

creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=GOOGLE_SCOPE)
gs_client = gspread.authorize(creds)
sheet = gs_client.open_by_key(SHEET_ID).sheet1

# === Helpers ===
def random_suffix(length=2):
    return ''.join(random.choices(string.ascii_lowercase, k=length))

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
    return status

# === MAIN FUNCTION WRAPPED HERE ===
def push_orders_main():
    # === Setup Tiger API ===
    config = TigerOpenClientConfig()
    client = TradeClient(config)

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

    # === Tally exit reasons ===
    filled_count, cancelled_count, lack_margin_count, unknown_count = 0, 0, 0, 0

    tiger_ids = set()
    for order in orders:
        oid = str(getattr(order, 'id', ''))
        if oid:
            tiger_ids.add(oid)

        status = str(getattr(order, "status", "")).split('.')[-1].upper()
        reason = str(getattr(order, "reason", "")).split('.')[-1] if getattr(order, "reason", "") else ""
        filled = getattr(order, "filled", 0)
        exit_reason = get_exit_reason(status, reason, filled)

        if exit_reason == "FILLED":
            filled_count += 1
        elif exit_reason == "CANCELLED":
            cancelled_count += 1
        elif exit_reason == "LACK_OF_MARGIN":
            lack_margin_count += 1
        else:
            unknown_count += 1

    print(f"✅ FILLED: {filled_count}")
    print(f"❌ CANCELLED: {cancelled_count}")
    print(f"🚫 LACK_OF_MARGIN: {lack_margin_count}")
    print(f"🟡 UNKNOWN: {unknown_count}")

        # === Push live positions to Firebase ===
    try:
        positions = client.get_positions(account="21807597867063647", sec_type=SegmentType.FUT)
        live_ref = db.reference("/live_positions")

        for pos in positions:
            symbol = getattr(pos, "symbol", "")
            quantity = getattr(pos, "quantity", 0)
            avg_cost = getattr(pos, "average_cost", 0.0)

            if symbol and quantity != 0:
                live_ref.child(symbol).set({
                    "quantity": quantity,
                    "average_cost": avg_cost,
                    "timestamp": datetime.utcnow().isoformat()
                })
                print(f"🟢 Updated /live_positions/: {symbol} = {quantity} @ {avg_cost}")
    except Exception as e:
        print(f"❌ Failed to update live positions: {e}")

    # === Push to Firebase ===
    for o in orders:
        order_id = str(getattr(o, 'id', ''))
        if not order_id:
            continue

        suffix = random_suffix()
        firebase_key = f"{order_id}-{suffix}"
        raw_contract = str(getattr(o, 'contract', ''))
        symbol = raw_contract.split('/')[0] if '/' in raw_contract else raw_contract
        status = str(getattr(o, 'status', '')).upper()
        reason = str(getattr(o, 'reason', '')).upper()
        filled = getattr(o, 'filled', 0)

        payload = {
            "order_id": order_id,
            "symbol": symbol,
            "action": str(getattr(o, 'action', '')).upper(),
            "quantity": getattr(o, 'quantity', 0),
            "filled": filled,
            "avg_fill_price": getattr(o, 'avg_fill_price', 0.0),
            "status": status,
            "reason": str(getattr(o, 'reason', '')) or '',
            "liquidation": getattr(o, 'liquidation', False),
            "timestamp": getattr(o, 'order_time', 0),
            "source": map_source(getattr(o, 'source', None)),
            "is_open": getattr(o, 'is_open', False),
            "exit_reason": get_exit_reason(status, reason, filled)
        }

        try:
            db.reference(f"/tiger_orders/{firebase_key}").set(payload)
            print(f"✅ Pushed to Firebase: {firebase_key}")
        except Exception as e:
            print(f"❌ Firebase push failed for {firebase_key}: {e}")

    # === FIFO Cleanup: Keep only N open trades ===
    try:
        # Rebuild FIFO stack of open orders
        orders_sorted = sorted(orders, key=lambda x: getattr(x, 'order_time', 0))
        stack = []

        for order in orders_sorted:
            action = str(getattr(order, 'action', '')).upper()
            quantity = getattr(order, 'filled', 0)
            if quantity == 0:
                continue
            if action == "BUY":
                stack.extend([order] * quantity)
            elif action == "SELL":
                stack = stack[quantity:]

        open_order_ids = set()
        for o in stack:
            oid = getattr(o, 'id', None)
            if oid:
                open_order_ids.add(str(oid))

        # Delete stale entries from Firebase
        open_ref = db.reference("/tiger_orders/")
        snapshot = open_ref.get() or {}

        for key, value in snapshot.items():
            firebase_oid = str(value.get("order_id", ""))
            if firebase_oid not in open_order_ids:
                print(f"🧹 Deleting old trade from Firebase: {key}")
                open_ref.child(key).delete()

                # === Friendly exit reason map ===
                reason_map = {
                    "trailing_tp_exit": "Trailing Take Profit",
                    "manual_close": "Manual Close",
                    "ema_flattening_exit": "EMA Flattening",
                    "liquidation": "Liquidation",
                    "LACK_OF_MARGIN": "Lack of Margin",
                    "FILLED": "FILLED",
                    "CANCELLED": "CANCELLED",
                    "EXPIRED": "Lack of Margin"
                }

                # === Log deleted ghost trade to Google Sheets ===
                try:
                    from pytz import timezone
                    now_nz = datetime.now(timezone("Pacific/Auckland"))
                    day_date = now_nz.strftime("%A %d %B %Y")

                    sheet.append_row([
                        day_date,
                        value.get("symbol", ""),
                        value.get("action", ""),
                        value.get("avg_fill_price", 0.0),
                        0.0,  # Exit price unknown
                        0.0,  # PnL is 0 for ghost trades
                        reason_map.get("LACK_OF_MARGIN", "LACK_OF_MARGIN"),
                        "",  # Entry time unknown
                        now.strftime("%Y-%m-%d %H:%M:%S"),
                        False  # trail_triggered
                    ])
                            # Also write ghost trade to CSV
                    row = {
                        "day_date": day_date,
                        "symbol": value.get("symbol", ""),
                        "direction": value.get("action", ""),
                        "entry_price": value.get("avg_fill_price", 0.0),
                        "exit_price": 0.0,
                        "pnl_dollars": 0.0,
                        "reason_for_exit": reason_map.get("LACK_OF_MARGIN", "LACK_OF_MARGIN"),
                        "entry_time": "",
                        "exit_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "trail_triggered": "NO"
                    }

                    # Write to CSV
                    file_exists = False
                    try:
                        with open(CLOSED_TRADES_FILE, 'r') as f:
                            file_exists = True
                    except FileNotFoundError:
                        pass

                    with open(CLOSED_TRADES_FILE, 'a', newline='') as file:
                        writer = csv.DictWriter(file, fieldnames=row.keys())
                        if not file_exists:
                            writer.writeheader()
                        writer.writerow(row)
                except Exception as e:
                    print(f"❌ Google Sheets log failed: {e}")

    except Exception as e:
        print(f"⚠️ FIFO cleanup failed: {e}")

        # === Patch in missing open trades from /live_positions/ ===
        live_positions_ref = db.reference("/live_positions")
        live_positions = live_positions_ref.get() or {}

        patched_count = 0

        for symbol, pos_data in live_positions.items():
            quantity = int(pos_data.get("quantity", 0))
            avg_cost = float(pos_data.get("average_cost", 0))
            action = "BUY" if quantity > 0 else "SELL"
            abs_qty = abs(quantity)

            open_ref = db.reference(f"/open_trades/{symbol}")
            current_trades = open_ref.get() or {}
            current_count = len(current_trades)

            if current_count < abs_qty:
                for _ in range(abs_qty - current_count):
                    patch_id = f"tigerpatch_{int(time.time()*1000)}_{random_suffix()}"
                    open_ref.child(patch_id).set({
                        "symbol": symbol,
                        "action": action,
                        "entry_price": avg_cost,
                        "order_id": "",  # ghost patch, no order ID
                        "source": "tigerpatch",
                        "trail_triggered": False
                    })
                    print(f"🐅 Patched open trade from /live_positions/: {patch_id}")
                    patched_count += 1

        print(f"✅ Finished patching {patched_count} ghost trades from live_positions.\n")

    # === Reconcile /open_trades/ against Tiger open orders ===
    try:
        print("🔍 Reconciling /open_trades/ against TigerTrade open orders...")

        open_trades_ref = db.reference("/open_trades/MGC2508")
        open_trades_snapshot = open_trades_ref.get() or {}

        deleted_count = 0
        tiger_order_map = {str(getattr(o, "id", "")): o for o in orders if getattr(o, "id", "")}

        for trade_id, trade_data in open_trades_snapshot.items():
            trade_order_id = str(trade_data.get("order_id", ""))

            if trade_order_id not in open_order_ids:
                print(f"🧹 Pruning stale /open_trades/ entry: {trade_id} (order_id={trade_order_id})")
                open_trades_ref.child(trade_id).delete()
                deleted_count += 1

                tiger_order = tiger_order_map.get(trade_order_id)
                if tiger_order:
                    status = str(getattr(tiger_order, "status", "")).split(".")[-1].upper()
                    reason = str(getattr(tiger_order, "reason", ""))
                    filled = getattr(tiger_order, "filled", 0)
                    exit_reason = get_exit_reason(status, reason, filled)
                else:
                    exit_reason = "manual_close"

                reason_map = {
                    "trailing_tp_exit": "Trailing Take Profit",
                    "manual_close": "Manual Close",
                    "ema_flattening_exit": "EMA Flattening",
                    "liquidation": "Liquidation",
                    "LACK_OF_MARGIN": "Lack of Margin",
                    "FILLED": "FILLED",
                    "CANCELLED": "CANCELLED",
                    "EXPIRED": "Lack of Margin"
                }
                friendly_reason = reason_map.get(exit_reason, exit_reason)

                now = datetime.now()
                day_date = now.strftime("%A %d %B %Y")

                sheet.append_row([
                    day_date,
                    trade_data.get("symbol", ""),
                    trade_data.get("action", ""),
                    trade_data.get("entry_price", 0.0),
                    0.0,
                    0.0,
                    friendly_reason,
                    trade_data.get("entry_timestamp", ""),
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    trade_data.get("trail_hit", False)
                ])

        print(f"✅ Open trades cleanup complete — {deleted_count} entries removed.")

    except Exception as e:
        print(f"❌ Error during /open_trades/ pruning: {e}")

# === SAFE ENTRY POINT ===
if __name__ == "__main__":
    push_orders_main()      