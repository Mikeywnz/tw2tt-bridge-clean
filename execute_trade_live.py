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
# Global cooldown tracker for orders
exit_cooldowns = {}

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
# 🟩 ENTRY TRADE LOGIC BLOCK
# ==========================
def execute_entry_trade(client, contract, symbol, action, quantity, db, order_cooldowns):
    print(f"🚀 Starting ENTRY trade logic for {symbol} {action} x {quantity}")

    cooldown_key = f"{symbol}_{action}_entry"
    now_ts = time.time()
    if order_cooldowns.get(cooldown_key, 0) > now_ts:
        print(f"⏳ Entry trade cooldown active for {cooldown_key}, skipping trade")
        return {"status": "COOLDOWN_SKIPPED", "reason": "Entry trade cooldown active"}

    order = Order(
        account=client.config.account,
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

    # Set entry cooldown
    order_cooldowns[cooldown_key] = now_ts + 60  # 60 seconds cooldown

    # Place for ghost trade archiving logic if needed, ONLY for entry trades

    return {
        "status": "SUCCESS",
        "order_id": order_id,
        "trade_type": "ENTRY",
        "symbol": symbol,
        "action": action,
        "quantity": quantity
    }


# ==========================
# 🟩 EXIT TRADE LOGIC BLOCK
# ==========================
def execute_exit_trade(client, contract, symbol, action, quantity, db, order_cooldowns):
    print(f"🚀 Starting EXIT trade logic for {symbol} {action} x {quantity}")

    # No ghost trade logic on exits

    order = Order(
        account=client.config.account,
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

    cooldown_key = f"{symbol}_{action}_exit"
    now_ts = time.time()
    order_cooldowns[cooldown_key] = now_ts + 60  # 60 seconds cooldown

    return {
        "status": "SUCCESS",
        "order_id": order_id,
        "trade_type": "EXIT",
        "symbol": symbol,
        "action": action,
        "quantity": quantity
    }


# ==========================
# 🟩 DISPATCHER FUNCTION
# ==========================
def place_trade(symbol, action, quantity, trade_type, db, order_cooldowns):
    symbol = symbol.upper()
    action = action.upper()
    contract = get_contract(symbol)

    if trade_type.upper() == "ENTRY":
        return execute_entry_trade(client, contract, symbol, action, quantity, db, order_cooldowns)
    elif trade_type.upper() == "EXIT":
        return execute_exit_trade(client, contract, symbol, action, quantity, db, order_cooldowns)
    else:
        print(f"❌ Unknown trade_type: {trade_type}")
        return {"status": "ERROR", "reason": f"Unknown trade_type {trade_type}"}


# ==========================
# 🟩 CLI MAIN ENTRYPOINT
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

    result = place_trade(symbol, action, quantity, trade_type, firebase_db, exit_cooldowns)

    print(f"🚀 Trade result: {result}")


if __name__ == "__main__":
    main()