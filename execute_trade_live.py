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

# ===================================
# 🟩 TIGER API CLIENT INIT (RUN ONCE)
# ===================================
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
# 🟩 PLACE ENTRY TRADE FUNCTION (Calls execute_entry_trade)
# ==========================
def place_entry_trade(symbol, action, quantity, db):
    global client
    print(f"[DEBUG] place_entry_trade called with client id: {id(client)}")
    print(f"[DEBUG] place_entry_trade client type: {type(client)}")

    symbol = symbol.upper()
    action = action.upper()
    contract = get_contract(symbol)
    print(f"[DEBUG] Contract fetched for symbol {symbol}: {contract}")

    order = Order(
        account=ACCOUNT,
        contract=contract,
        action=action,
        order_type='MKT',
        quantity=quantity
    )
    print(f"[DEBUG] Created market order: {symbol} {action} {quantity}")

    try:
        response = client.place_order(order)
        print(f"[DEBUG] Tiger order response (entry): {response}")
    except Exception as e:
        print(f"[ERROR] Exception placing entry order: {e}")
        return {"status": "ERROR", "reason": str(e)}

    order_id = None
    if isinstance(response, dict):
        order_id = response.get("id")
    elif isinstance(response, (str, int)) and str(response).isdigit():
        order_id = str(response)

    print(f"[DEBUG] Parsed order_id: {order_id}")

    if not order_id:
        print("[ERROR] Failed to parse order ID for entry trade")
        return {"status": "REJECTED", "reason": "No order ID returned from Tiger"}

    print(f"[INFO] Entry order placed with order_id: {order_id}")

    try:
        max_retries = 5
        retry_count = 0
        transactions = client.get_transactions(order_id=order_id)

        while len(transactions) == 0 and retry_count < max_retries:
            print(f"[DEBUG] No transactions found for order_id {order_id}, retrying in 2 seconds...")
            time.sleep(2)
            transactions = client.get_transactions(order_id=order_id)
            retry_count += 1

        if len(transactions) == 0:
            print(f"[ERROR] Failed to fetch transactions after {max_retries} retries for order_id {order_id}")
            return {"status": "ERROR", "reason": "No transactions found"}

        print(f"New order is {transactions}")

        action = transactions[0].action
        quantity = transactions[0].filled_quantity
        filled_price = transactions[0].filled_price
        transaction_time = transactions[0].transacted_at

        tx_dict = {
            "status": "SUCCESS",
            "order_id": order_id,
            "trade_type": "LONG_ENTRY" if action == "BUY" else "SHORT_ENTRY",
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "filled_price": filled_price,
            "transaction_time": transaction_time,
        }
        print(f"[DEBUG] Transaction dict prepared: {tx_dict}")
        return tx_dict

    except Exception as e:
        print(f"[ERROR] Failed to fetch transaction details for order_id {order_id}: {e}")
        return {
            "status": "ERROR",
            "reason": "Failed to fetch transaction details",
            "order_id": order_id
        }
# ======================================================================================
# 🟩 PLACE EXIT TRADE FUNCTION (Calls execute_exit_trade, fetches full transaction info)
# ======================================================================================
def place_exit_trade(symbol, action, quantity, db):
    global client
    print(f"[DEBUG] place_exit_trade called with client id: {id(client)}")
    print(f"[DEBUG] place_exit_trade client type: {type(client)}")

    symbol = symbol.upper()
    action = action.upper()
    contract = get_contract(symbol)

    # --- Place the exit market order ---
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

    # --- Fetch transaction details matching this exit order_id ---
    try:
        max_retries = 5
        retry_count = 0
        transactions = client.get_transactions(order_id=order_id)

        while len(transactions) == 0 and retry_count < max_retries:
            print(f"[DEBUG] No transactions found for order_id {order_id}, retrying in 2 seconds...")
            time.sleep(2)
            transactions = client.get_transactions(order_id=order_id)
            retry_count += 1

        if len(transactions) == 0:
            print(f"[ERROR] Failed to fetch transactions after {max_retries} retries for order_id {order_id}")
            return {"status": "ERROR", "reason": "No transactions found"}

        print(f"New order is {transactions}")

        action = transactions[0].action
        quantity = transactions[0].filled_quantity
        filled_price = transactions[0].filled_price
        transaction_time = transactions[0].transacted_at

        tx_dict = {
            "status": "SUCCESS",
            "order_id": order_id,
            "trade_type": "FLATTENING_SELL" if action == "SELL" else "FLATTENING_BUY" if action == "BUY" else "EXIT",
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "filled_price": filled_price,
            "transaction_time": transaction_time,
        }
        print(f"[DEBUG] Transaction dict prepared: {tx_dict}")
        return tx_dict

    except Exception as e:
        print(f"[ERROR] Failed to fetch transaction details for order_id {order_id}: {e}")
        return {
            "status": "ERROR",
            "reason": "Failed to fetch transaction details",
            "order_id": order_id
        }

# ===============================================
# 🟩 CLI MAIN ENTRYPOINT (No cooldown dictionary)
# ===============================================
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

    # Use place_entry_trade or place_exit_trade depending on trade_type
    if trade_type in ["LONG_ENTRY", "SHORT_ENTRY"]:
        result = place_entry_trade(symbol, action, quantity, firebase_db)
    elif trade_type in ["FLATTENING_SELL", "FLATTENING_BUY"]:
        result = place_exit_trade(symbol, action, quantity, firebase_db)
    else:
        print(f"❌ Unknown trade_type {trade_type}")
        sys.exit(1)

    print(f"🚀 Trade result: {result}")


if __name__ == "__main__":
    main()