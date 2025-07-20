from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType
import firebase_admin
from firebase_admin import credentials, db
import random
import string
from datetime import datetime, timedelta

# === Firebase Init ===
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/'
})

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
    except Exception as e:
        print(f"⚠️ FIFO cleanup failed: {e}")

# === SAFE ENTRY POINT ===
if __name__ == "__main__":
    push_orders_main()