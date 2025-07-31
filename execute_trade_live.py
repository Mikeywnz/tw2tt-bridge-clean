#=========================  EXECUTE_TRADE_LIVE  ================================
import sys
import os
import json
import time
from datetime import datetime
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.trade.domain.contract import Contract
from tigeropen.trade.domain.order import Order
import firebase_active_contract

# ==========================
# 🟩 GREEN PATCH START: Archive Ghost Trade Immediately
# ==========================
import firebase_admin
from firebase_admin import db

def archive_ghost_trade(trade_id, trade_data):
    try:
        ghost_ref = db.reference("/ghost_trades_log")
        ghost_ref.child(trade_id).set(trade_data)
        print(f"✅ Archived ghost trade {trade_id} to ghost_trades_log")
    except Exception as e:
        print(f"❌ Failed to archive ghost trade {trade_id}: {e}")
# ==========================
# 🟩 GREEN PATCH END
# ==========================

def place_trade(symbol, action, quantity):
   # action = action.upper()
   # if action not in ("BUY", "SELL"):
   #     raise ValueError(f"Invalid action passed to place_trade: {action}")
    # Initialize status to avoid unbound local variable error
    status = None  # <<< GREEN PATCH: Initialize status early to avoid error
    
    # Ignore passed symbol; fetch active contract from Firebase instead
    import firebase_active_contract
    symbol = firebase_active_contract.get_active_contract()
    if not symbol:
        raise ValueError("No active contract symbol found in Firebase")
    symbol = symbol.upper()
    action = action.upper()
    print(f"📂 Executing Trade → Symbol: {symbol}, Action: {action}")

    # === Load Tiger Config ===
    try:
        config = TigerOpenClientConfig()  # This is critical do not change this on this version if tiger SDK
        config.env = 'PROD'
        config.language = 'en_US'

        if not config.account:
            raise ValueError("Tiger config loaded but account is missing or blank.")

        client = TradeClient(config)

    except Exception as e:
        print(f"❌ Failed to load Tiger API config or initialize client: {e}")
        raise e

    # 🔒 === LOCKED: Define Futures Contract (do not modify this block) ===
    contract = Contract()
    contract.symbol = symbol
    contract.sec_type = 'FUT'
    contract.currency = 'USD'
    contract.exchange = 'CME'

    # 🔒 === LOCKED: Create Order (exact format TigerTrade requires) ===
    order = Order(
        account=config.account,
        contract=contract,
        action=action
    )
    order.order_type = 'MKT'  # 🔒 Must be 'MKT' — Tiger's required market order code
    order.quantity = quantity

    # === Submit Order ===
    try:
        response = client.place_order(order)
        print("🐯 Full Tiger order response:", response)

        # Extract order ID robustly
        order_id = None
        if isinstance(response, dict):
            order_id = response.get("id", None)
        elif isinstance(response, str) and response.isdigit():
            order_id = response
        elif isinstance(response, int):
            order_id = str(response)

        print(f"📛 Parsed order_id: {order_id}")

        # 🟢 PATCH: Retry loop to fetch matching transaction, polling every 3 sec up to 3 times
        matched_tx = None
        if order_id:
            for attempt in range(3):  # Retry 3 times
                transactions = client.get_transactions(account=config.account, symbol=symbol, limit=20)
                matched_tx = next((tx for tx in transactions if str(tx.order_id) == str(order_id)), None)
                if matched_tx:
                    print(f"✅ Found matching transaction on attempt {attempt+1}")
                    break
                else:
                    print(f"⏳ Transaction not found on attempt {attempt+1}, retrying...")
                    time.sleep(3)  # Wait 3 seconds before next attempt
        else:
            print("🛑 No order_id parsed from response → rejecting trade")
            # Assign status here before returning
            status = "REJECTED"
            return {
                "status": "REJECTED",
                "order_id": None,
                "reason": "No order ID from Tiger response",
                "trade_status": status,
                "trade_state": "closed",
                "trade_type": ""
            }

        if matched_tx:
            filled_qty = getattr(matched_tx, "filled_quantity", 0)
            # Determine status and trade_state
            status = "FILLED" if filled_qty > 0 else "REJECTED"
            trade_state = "open" if filled_qty > 0 else "closed"

            # ==========================
            # 🟩 GREEN PATCH START: Strict Ghost Trade Rejection in place_trade()
            # ==========================

            # Place this inside place_trade(), right after you retrieve matched_tx and filled_qty:

            ghost_statuses = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
            trade_status = getattr(matched_tx, "status", "").upper()

         # ==========================
            # 🟩 GREEN PATCH START: Call archive_ghost_trade on ghost detection
            # ==========================

            if filled_qty == 0 and trade_status in ghost_statuses:
                print(f"⏭️ Rejected ghost trade detected: status={trade_status}, filled_qty=0")
                archive_ghost_trade(order_id, {
                    "trade_status": trade_status,
                    "filled_quantity": filled_qty,
                    "trade_state": "closed",
                    "trade_type": "",
                    "raw_transaction": matched_tx
                })
                return {
                    "status": "skipped",
                    "reason": "ghost trade - zero fill with bad status",
                    "trade_status": trade_status,
                    "trade_state": "closed",
                    "trade_type": ""
                }

            # ==========================
            # 🟩 GREEN PATCH END
            # ==========================
            # Determine trade_type based on action and position
            if filled_qty > 0:
                trade_type = "LONG_ENTRY" if action == "BUY" else "SHORT_ENTRY"
            else:
                trade_type = ""

            if filled_qty > 0:
                print(f"✅ Order {order_id} filled with quantity {filled_qty}")
                # Return full dictionary including original TigerTrade data plus new fields
                return {
                    "status": "SUCCESS",
                    "order_id": order_id,
                    "filled_quantity": filled_qty,
                    "filled_price": getattr(matched_tx, "filled_price", None),
                    "filled_amount": getattr(matched_tx, "filled_amount", None),
                    "transacted_at": getattr(matched_tx, "transacted_at", None),
                    "transaction_time": getattr(matched_tx, "transaction_time", None),
                    # New interpreted fields:
                    "trade_status": status,
                    "trade_state": trade_state,
                    "trade_type": trade_type,
                    # include the full matched_tx object if you want raw data as well
                    "raw_transaction": matched_tx
                }
            else:
                print(f"🛑 Order {order_id} has zero fill quantity → treated as rejected")
                return {
                    "status": "REJECTED",
                    "order_id": order_id,
                    "reason": "Zero fill quantity",
                    "trade_status": status,
                    "trade_state": trade_state,
                    "trade_type": trade_type,
                    "raw_transaction": matched_tx
                }
        else:
            print(f"🛑 No matching transaction found for order {order_id} → treated as rejected")
            # Assign status here before returning
            status = "REJECTED"
            return {
                "status": "REJECTED",
                "order_id": order_id,
                "reason": "No matching transaction found",
                "trade_status": status,
                "trade_state": "closed",
                "trade_type": ""
            }

    except Exception as e:
        print("❌ Tiger API Exception raised:")
        print(e)
        if hasattr(e, 'args') and len(e.args) > 0:
            print("🧪 Tiger error details:", e.args[0])
        import traceback
        traceback.print_exc()
        raise e

    # === Get Live Price from local file ===
    live_price = 0.0
    try:
        with open(os.path.join(os.path.dirname(__file__), 'live_prices.json')) as f:
            live_data = json.load(f)
            data = live_data.get(symbol)
            if isinstance(data, dict):
                live_price = float(data.get("price", 0.0))
            elif isinstance(data, (float, int)):
                live_price = float(data)
    except Exception as e:
        print("⚠️ Could not read live_prices.json:", e)

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    print(f"✅ Trade confirmed filled at approx. ${live_price} 🕒 timestamp {timestamp}", flush=True)
    print(f"✅ Tiger Order ID: {order_id}", flush=True)

    return {
        "status": "SUCCESS",
        "order_id": order_id,
        # Returning these for consistency, but may be incomplete if no matched_tx found
        "filled_quantity": 0,
        "filled_price": None,
        "filled_amount": None,
        "transacted_at": None,
        "transaction_time": None,
        # New interpreted fields as fallback
        "trade_status": "FILLED",
        "trade_state": "open",
        "trade_type": "UNKNOWN"
    }

#=====  END OF SCRIPT =====