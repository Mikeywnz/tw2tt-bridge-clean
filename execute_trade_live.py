#=========================  EXECUTE_TRADE_LIVE – REFACTORED ================================
import sys
import os
import json
import time
from datetime import datetime
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.trade.domain.contract import Contract
from tigeropen.trade.domain.order import Order
import firebase_admin
from firebase_admin import db

# ---------------------------
# your Tiger Trade account number
ACCOUNT = "21807597867063647"  


# Firebase DB reference shortcut
firebase_db = db

# ==========================
# 🟩 TIGER API CLIENT INIT (RUN ONCE)
# ==========================
try:
    config = TigerOpenClientConfig()  # Locked: do not modify config loading
    config.env = 'PROD'
    config.language = 'en_US'

    if not config.account:
        raise ValueError("Tiger config loaded but account is missing or blank.")

    client = TradeClient(config)
    print("✅ Tiger API client initialized successfully")
except Exception as e:
    print(f"❌ Failed to load Tiger API config or initialize client: {e}")
    sys.exit(1)
    print(f"[DEBUG] Initialized client object: {client}")
    print(f"[DEBUG] client type: {type(client)}")
    print(f"[DEBUG] client has config: {'config' in dir(client)}")
    print(f"[DEBUG] client.config.account: {getattr(client.config, 'account', None)}")
    print(f"[DEBUG] client id: {id(client)}")


# ==========================
# 🟩 CONTRACT CREATION HELPER
# ==========================
def get_contract(symbol: str):
    contract = Contract()
    contract.symbol = symbol
    contract.sec_type = 'FUT'
    contract.currency = 'USD'
    contract.exchange = 'CME'
    return contract


# ==========================
# 🟩 ARCHIVE GHOST TRADE UTILITY
# ==========================
def archive_ghost_trade(trade_id, trade_data):
    try:
        ghost_ref = db.reference("/ghost_trades_log")
        ghost_ref.child(trade_id).set(trade_data)
        print(f"✅ Archived ghost trade {trade_id} to ghost_trades_log")
    except Exception as e:
        print(f"❌ Failed to archive ghost trade {trade_id}: {e}")


# ==========================
# 🟩 ENTRY TRADE LOGIC BLOCK (No cooldown, single try)
# ==========================
def execute_entry_trade(client, contract, symbol, action, quantity, db):
    print(f"🚀 Starting ENTRY trade logic for {symbol} {action} x {quantity}")
    print(f"[DEBUG] execute_entry_trade client id: {id(client)}")  # or execute_exit_trade similarly
    print(f"[DEBUG] execute_entry_trade client type: {type(client)}")
    print(f"[DEBUG] execute_entry_trade has config: {'config' in dir(client)}")

    order = Order(
        account=ACCOUNT,
        contract=contract,
        action=action,
        order_type='MKT',
        quantity=quantity
    )
    print(f"📦 Placing ENTRY market order: {symbol} {action} {quantity}")

    try:
        response = client.place_order(order)
        print(f"🐯 Tiger order response (entry): {response}")
    except Exception as e:
        print(f"❌ Exception placing entry order: {e}")
        return {"status": "ERROR", "reason": str(e)}

    order_id = None
    if isinstance(response, dict):
        order_id = response.get("id")
    elif isinstance(response, (str, int)) and str(response).isdigit():
        order_id = str(response)

    if not order_id:
        print("🛑 Failed to parse order ID for entry trade")
        return {"status": "REJECTED", "reason": "No order ID returned from Tiger"}

    print(f"✅ Entry order placed with order_id: {order_id}")

    # ==========================
    # 🟩 GHOST TRADE DOOR: Archive ghost trades immediately after order placement
    # ==========================

    # Determine if the trade is a ghost trade (e.g., rejected, expired, no fill)
    trade_status = getattr(response, "status", "").upper() if hasattr(response, "status") else ""
    ghost_statuses = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}

    filled_qty = 0
    if hasattr(response, "filled_quantity"):
        filled_qty = response.filled_quantity
    elif isinstance(response, dict):
        filled_qty = response.get("filled_quantity", 0)

    if filled_qty == 0 and trade_status in ghost_statuses:
        print(f"⏭️ Rejected ghost trade detected: status={trade_status}, filled_qty=0")
        archive_ghost_trade(order_id, {
            "trade_status": trade_status,
            "filled_quantity": filled_qty,
            "trade_state": "closed",
            "trade_type": "",
            "raw_response": response
        })
        return {
            "status": "skipped",
            "reason": "ghost trade - zero fill with bad status",
            "trade_status": trade_status,
            "trade_state": "closed",
            "trade_type": ""
        }

    return {
        "status": "SUCCESS",
        "order_id": order_id,
        "trade_type": "ENTRY",
        "symbol": symbol,
        "action": action,
        "quantity": quantity
    }


# ==========================
# 🟩 EXIT TRADE LOGIC BLOCK (No cooldown, single try)
# ==========================
def execute_entry_trade(client, contract, symbol, action, quantity, db):
    print(f"🚀 Starting EXIT trade logic for {symbol} {action} x {quantity}")
    print(f"[DEBUG] execute_exit_trade client id: {id(client)}")
    print(f"[DEBUG] execute_exit_trade client type: {type(client)}")
    print(f"[DEBUG] execute_exit_trade has config: {'config' in dir(client)}")
   

    # No ghost trade logic on exits

    order = Order(
        account=ACCOUNT,
        contract=contract,
        action=action,
        order_type='MKT',
        quantity=quantity
    )
    print(f"📦 Placing EXIT market order: {symbol} {action} {quantity}")

    try:
        response = client.place_order(order)
        print(f"🐯 Tiger order response (exit): {response}")
    except Exception as e:
        print(f"❌ Exception placing exit order: {e}")
        return {"status": "ERROR", "reason": str(e)}

    order_id = None
    if isinstance(response, dict):
        order_id = response.get("id")
    elif isinstance(response, (str, int)) and str(response).isdigit():
        order_id = str(response)

    if not order_id:
        print("🛑 Failed to parse order ID for exit trade")
        return {"status": "REJECTED", "reason": "No order ID returned from Tiger"}

    print(f"✅ Exit order placed with order_id: {order_id}")

    # Mark exit in progress for FIFO matching in Firebase
    try:
        open_trades_ref = db.reference(f"/open_active_trades/{symbol}")
        open_trades = open_trades_ref.get() or {}
        for trade_id, trade in open_trades.items():
            if trade.get('action') != action and not trade.get('exited'):
                open_trades_ref.child(trade_id).update({
                    "exit_in_progress": True,
                    "contracts_remaining": 0,
                    "exit_order_id": order_id,
                    "exit_action": action,
                    "exit_filled_qty": quantity
                })
                print(f"[INFO] Marked trade {trade_id} as exit_in_progress for FIFO matching")
                break
    except Exception as e:
        print(f"⚠️ Failed to update exit_in_progress in Firebase: {e}")

    return {
        "status": "SUCCESS",
        "order_id": order_id,
        "trade_type": "EXIT",
        "symbol": symbol,
        "action": action,
        "quantity": quantity
    }


# ==========================
# 🟩 DISPATCHER FUNCTION (Detailed trade type routing)
# ==========================
def place_trade(symbol, action, quantity, trade_type, db):
    global client  # Use the globally initialized client
    print(f"[DEBUG] place_trade called with client id: {id(client)}")
    print(f"[DEBUG] place_trade client type: {type(client)}")

    symbol = symbol.upper()
    action = action.upper()
    contract = get_contract(symbol)

    # Use detailed trade_type strings for routing
    if trade_type in ["LONG_ENTRY", "SHORT_ENTRY"]:
        return execute_entry_trade(client, contract, symbol, action, quantity, db)
    elif trade_type in ["FLATTENING_BUY", "FLATTENING_SELL"]:
        return execute_exit_trade(client, contract, symbol, action, quantity, db)
    else:
        print(f"❌ Unknown trade_type: {trade_type}")
        return {"status": "ERROR", "reason": f"Unknown trade_type {trade_type}"}


# ==========================
# 🟩 CLI MAIN ENTRYPOINT (No cooldown dictionary)
# ==========================
def main():
    if len(sys.argv) < 5:
        print("❌ Usage: execute_trade_live.py SYMBOL ACTION QUANTITY TRADE_TYPE")
        sys.exit(1)

    symbol = sys.argv[1].upper()
    action = sys.argv[2].upper()
    try:
        quantity = int(sys.argv[3])
    except ValueError:
        print(f"❌ Invalid quantity '{sys.argv[3]}'; must be an integer")
        sys.exit(1)

    trade_type = sys.argv[4].upper()

    print(f"🚀 CLI launch: Placing {trade_type} trade with symbol={symbol}, action={action}, quantity={quantity}")

    result = place_trade(symbol, action, quantity, trade_type, firebase_db)

    print(f"🚀 Trade result: {result}")


if __name__ == "__main__":
    main()