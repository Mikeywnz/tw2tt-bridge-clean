# ========================= MONITOR_TRADES_LOOP - Segment 1 ================================
import firebase_admin
from firebase_admin import credentials, initialize_app, db
import time
from datetime import datetime, timezone, timedelta
import requests
import subprocess
import firebase_active_contract
import os
from execute_trade_live import place_exit_trade
from pytz import timezone

NZ_TZ = timezone("Pacific/Auckland")
processed_exit_order_ids = set()
last_cleanup_timestamp = None

#Important: Do NOT set trade_type to "closed". Use 'status' or 'trade_state' to indicate closure.

#================================
# 🟩 FIREBASE INITIALIZATION======
#================================

# === Firebase Key ===
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
cred = credentials.Certificate(firebase_key_path)

# === Firebase Initialization ===
if not firebase_admin._apps:
    firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
    cred = credentials.Certificate(firebase_key_path)
    initialize_app(cred, {
        'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
    })

firebase_db = db

# ==============================================
# 🟩 HELPER: Reason Map for Friendly Definitions
# ==============================================
REASON_MAP = {
    "trailing_tp_exit": "Trailing Take Profit",
    "manual_close": "Manual Close",
    "ema_flattening_exit": "EMA Flattening",
    "liquidation": "Liquidation",
    "LACK_OF_MARGIN": "Lack of Margin",
    "CANCELLED": "Cancelled",
    "EXPIRED": "Lack of Margin",
}

# =========================================
# 🟩 HELPER: Load Live Prices from Firebase
# =========================================
def load_live_prices():
    return db.reference("live_prices").get() or {}

# ===========================================================================================
# 🟩 HELPER: Update Trade on Exit Fill (Exit Order Confirmation Handler) with P&L Calculation
# ===========================================================================================
def update_trade_on_exit_fill(firebase_db, symbol, exit_order_id, exit_action, filled_qty, fill_price=None, fill_time=None):
    global processed_exit_order_ids
    if exit_order_id in processed_exit_order_ids:
        print(f"[DEBUG] Exit order {exit_order_id} already processed, skipping update.")
        return True
    processed_exit_order_ids.add(exit_order_id)

    print(f"[DEBUG] update_trade_on_exit_fill() called for exit_order_id={exit_order_id}")

    open_active_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
    open_trades = open_active_trades_ref.get() or {}

    # Find trade with matching exit_order_id
    matching_order_id = None
    for order_id, trade in open_trades.items():
        if trade.get("exit_order_id") == exit_order_id:
            matching_order_id = order_id
            print(f"[DEBUG] Found matching trade {order_id} for exit_order_id {exit_order_id}")
            break

    if not matching_order_id:
        print(f"[WARN] No matching trade found with exit_order_id {exit_order_id} on symbol {symbol}")
        return False

    update_data = {
        "exit_filled_qty": filled_qty,
    }
    if fill_price is not None:
        update_data["exit_fill_price"] = fill_price
    if fill_time is not None:
        update_data["exit_fill_time"] = fill_time

    # === Calculate Realized P&L ===
    try:
        entry_price = trade.get("filled_price")
        entry_qty = trade.get("filled_quantity")
        if entry_price is None or entry_qty is None:
            print(f"[WARN] Missing entry price or quantity for trade {matching_order_id}; skipping P&L calculation.")
        else:
            # Calculate P&L depending on exit action
            # Assume simple formula: (Exit - Entry) * Qty for long, reversed for short
            if exit_action == "SELL":
                pnl = (fill_price - entry_price) * filled_qty
            elif exit_action == "BUY":
                pnl = (entry_price - fill_price) * filled_qty
            else:
                pnl = 0.0
                print(f"[WARN] Unknown exit_action '{exit_action}' for P&L calculation.")
            
            update_data["realized_pnl"] = pnl
            print(f"[INFO] Calculated realized P&L for trade {matching_order_id}: {pnl:.2f}")
    except Exception as e:
        print(f"[ERROR] Exception during P&L calculation for trade {matching_order_id}: {e}")

    trade_ref = open_active_trades_ref.child(matching_order_id)
    print(f"[DEBUG] Updating fill details and P&L for trade {matching_order_id}")

    try:
        trade_ref.update(update_data)
        print(f"[INFO] Updated trade {matching_order_id} with fill data and P&L in Firebase")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to update trade {matching_order_id}: {e}")
        return False

# ========================================================
# 🟩 Helper: Load_trailing_tp_settings() 
# ========================================================

def load_trailing_tp_settings():
    try:
        ref = db.reference('/trailing_tp_settings')
        cfg = ref.get() or {}

        if cfg.get("enabled", False):
            trigger_points = float(cfg.get("trigger_points", 14.0))
            offset_points = float(cfg.get("offset_points", 5.0))
        else:
            trigger_points = 14.0
            offset_points = 5.0

    except Exception as e:
        print(f"[WARN] Failed to fetch trailing settings, using defaults: {e}")
        trigger_points = 14.0
        offset_points = 5.0

    return trigger_points, offset_points 

#=======================================
# Helper: Check if trade is archived
#=======================================

def is_archived_trade(order_id, firebase_db):
    archived_ref = firebase_db.reference("/archived_trades_log")
    archived_trades = archived_ref.get() or {}
    return order_id in archived_trades

#=============================================
# 🟢  HELPER: to Archive trade before deletion
#=============================================

def archive_trade(symbol, trade):
    order_id = trade.get("order_id")
    if not order_id:
        print(f"❌ Cannot archive trade without order_id")
        return False
    try:
        archive_ref = db.reference(f"/archived_trades_log/{order_id}")
        if "trade_type" not in trade or not trade["trade_type"]:
            trade["trade_type"] = "UNKNOWN"
        archive_ref.set(trade)
        print(f"[DEBUG] Archiving trade {order_id} with trade_type: {trade.get('trade_type')}")
        return True
    except Exception as e:
        print(f"❌ Failed to archive trade {order_id}: {e}")
        return False

# ==============================================
# 🟩 HELPER: archive_and_delete_matched_trades()
# ==============================================

def archive_and_delete_matched_trades(symbol, matched_trades):
    """
    Archive matched trades and delete them from open_active_trades in Firebase.

    Args:
        symbol (str): Symbol of the trades (e.g., "MGC2508")
        matched_trades (list): List of trade dicts to archive and delete
    """
    open_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")

    for trade in matched_trades:
        order_id = trade.get("order_id")
        if not order_id:
            print("⚠️ Skipping trade with no order_id during archive/delete")
            continue

        # Archive the trade first
        success = archive_trade(symbol, trade)
        if not success:
            print(f"❌ Failed to archive trade {order_id}; skipping deletion")
            continue

        # Delete from open trades
        try:
            open_trades_ref.child(order_id).delete()
            print(f"✅ Archived and deleted trade {order_id} for symbol {symbol}")
        except Exception as e:
            print(f"❌ Failed to delete trade {order_id} from Firebase: {e}")

#=============================
# Firebase open trades handler
#=============================

def load_open_trades(symbol):
    try:
        ref = db.reference(f"/open_active_trades/{symbol}")
        data = ref.get() or {}
        trades = []
        if isinstance(data, dict):
            for order_id, td in data.items():
                td['order_id'] = order_id
                trades.append(td)
        print(f"🔄 Loaded {len(trades)} open trades from Firebase.")
        return trades
    except Exception as e:
        print(f"❌ Failed to fetch open trades: {e}")
        return []

def save_open_trades(symbol, trades):
    ref = db.reference(f"/open_active_trades/{symbol}")
    try:
        for t in trades:
            order_id = t.get("order_id")
            if not order_id:
                continue
            ref.child(order_id).update(t)
            print(f"✅ Open Active Trade {order_id} saved to Firebase.")
    except Exception as e:
        print(f"❌ Failed to save open trades to Firebase: {e}")

# ==========================================================
# 🟩 TRAILING TP AND EXIT PROCESSING WITH place_exit_trade()
# ==========================================================

def process_trailing_tp_and_exits(active_trades, prices, trigger_points, offset_points, exit_in_progress):
    print(f"[DEBUG] process_trailing_tp_and_exits() called with {len(active_trades)} active trades")
    for i, trade in enumerate(active_trades):
        print(trade)
        if not trade or not isinstance(trade, dict):
            continue
        if trade.get("status") == "closed":
            print(f"🔒 Skipping closed trade {trade.get('order_id')}")
            continue
        order_id = trade.get('order_id', 'unknown')
        print(f"🔄 Processing trade {order_id}")
        if trade.get('exited') or order_id in exit_in_progress:
            continue

        symbol = trade['symbol']
        direction = 1 if trade['action'] == 'BUY' else -1
        current_price = prices.get(symbol, {}).get('price') if isinstance(prices.get(symbol), dict) else prices.get(symbol)

        if current_price is None:
            print(f"⚠️ No price for {symbol} — skipping {order_id}")
            continue

        entry = trade.get('filled_price')
        if entry is None:
            print(f"❌ Trade {order_id} missing filled_price, skipping.")
            continue

        if not trade.get('trail_hit'):
            trade['trail_peak'] = entry
            trigger_price = entry + trigger_points if direction == 1 else entry - trigger_points
            print(f"[DEBUG] TP trigger price for trade {order_id} set at {trigger_price:.2f}")
            if (direction == 1 and current_price >= trigger_price) or (direction == -1 and current_price <= trigger_price):
                trade['trail_hit'] = True
                trade['trail_peak'] = current_price
                print(f"[INFO] TP trigger HIT for trade {order_id} at price {current_price:.2f}")

                try:
                    open_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
                    open_trades_ref.child(order_id).update({
                        "trail_hit": True,
                        "trail_peak": current_price
                    })
                except Exception as e:
                    print(f"❌ Failed to update trail_hit in Firebase for trade {order_id}: {e}")

        if trade.get('trail_hit'):
            prev_peak = trade.get('trail_peak', entry)
            if direction == 1:   # LONG
                new_peak = max(prev_peak, current_price)
            else:                # SHORT
                new_peak = min(prev_peak, current_price)

            if new_peak != prev_peak:
                print(f"[DEBUG] New trail peak for {order_id}: {new_peak:.2f} (prev: {prev_peak:.2f})")
                trade['trail_peak'] = new_peak
                try:
                    open_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
                    open_trades_ref.child(order_id).update({"trail_peak": new_peak})
                except Exception as e:
                    print(f"❌ Failed to update trail_peak for {order_id}: {e}")
            else:
                print(f"[DEBUG] Trail peak unchanged for {order_id}: {trade['trail_peak']:.2f}")

            buffer_amt = offset_points
            print(f"[DEBUG] Buffer amount for {order_id}: {buffer_amt:.2f}")

            # Exit trigger check
            if (direction == 1 and current_price <= trade['trail_peak'] - buffer_amt) or \
            (direction == -1 and current_price >= trade['trail_peak'] + buffer_amt):
                print(f"[INFO] Trailing TP EXIT condition met for {order_id}: price={current_price:.2f}, peak={trade['trail_peak']:.2f}, buffer={buffer_amt:.2f}")

                try:
                    result = place_exit_trade(symbol, 'SELL' if trade['action'] == 'BUY' else 'BUY', 1, firebase_db)

                    if result.get("status") == "SUCCESS":
                        print(f"📤 Exit order placed successfully for trade {order_id}")

                        # Update Firebase with exit fill details
                        update_trade_on_exit_fill(firebase_db, symbol, result["order_id"], result["action"], result["quantity"], result.get("filled_price"), result.get("transaction_time"))

                        # Patch: Update trade_type in Firebase for this trade
                        try:
                            open_trades_ref.child(order_id).update({
                                "trade_type": result.get("trade_type", "")
                            })
                            print(f"✅ Updated trade_type to {result.get('trade_type')} for trade {order_id}")
                        except Exception as e:
                            print(f"❌ Failed to update trade_type for trade {order_id}: {e}")

                        exit_in_progress.add(order_id)
                        trade['exit_in_progress'] = True
                        open_trades_ref.child(order_id).update({
                            "exit_in_progress": True,
                            "exit_order_id": result["order_id"]
                        })
                    else:
                        print(f"❌ Exit order failed for trade {order_id}: {result}")

                        print(f"📤 Exit order placed successfully for trade {order_id}: {result}")
                except Exception as e:
                    print(f"❌ Exception placing exit trade for trade {order_id}: {e}")

        # Update the trade back in active_trades list (in-place)
        active_trades[i] = trade

    return active_trades

# ==============================================
# 🟩 FIFO MATCH AND FLATTEN WITH FIREBASE UPDATE
# ==============================================

def fifo_match_and_flatten(active_trades, symbol, firebase_db):
    print(f"[DEBUG] fifo_match_and_flatten() called with {len(active_trades)} active trades")
    # ===== Archive and Delete Matched Trades =====
    matched_trades = [t for t in active_trades if t.get('exited') or t.get('trade_state') == 'closed']
    print(f"[DEBUG] Found {len(matched_trades)} matched trades to archive and delete")
    archive_and_delete_matched_trades(symbol, matched_trades)
    print(f"[DEBUG] Completed archiving and deleting matched trades")

    # Remove matched trades from active_trades list to avoid reprocessing
    active_trades = [t for t in active_trades if t not in matched_trades]
    print(f"[DEBUG] {len(active_trades)} trades remain active after cleanup")

    exit_trades = [t for t in active_trades if t.get('exit_in_progress') and not t.get('exited')]
    open_trades = [t for t in active_trades if not t.get('exited') and not t.get('exit_in_progress')]

    # Sort open trades by entry time ascending for FIFO matching
    open_trades.sort(key=lambda t: t.get('entry_timestamp', t['order_id']))

    print(f"[DEBUG] Found {len(exit_trades)} exit trades and {len(open_trades)} open trades for matching")

    for exit_trade in exit_trades:
        matched = False
        for open_trade in open_trades:
            print(f"Trying to match exit trade {exit_trade.get('order_id')} with open trade {open_trade.get('order_id')}")
            if not open_trade.get('exited'):
                open_trade['exited'] = True
                open_trade['trade_state'] = 'closed'
                open_trade['contracts_remaining'] = 0

                # Calculate PnL here
                try:
                    entry_price = open_trade.get("filled_price")
                    exit_price = exit_trade.get("filled_price")
                    quantity = exit_trade.get("exit_filled_qty")
                    if not quantity or quantity <= 0:
                        quantity = 1

                    pnl = 0.0
                    if entry_price is not None and exit_price is not None:
                        if open_trade.get('action') == 'BUY':
                            pnl = (exit_price - entry_price) * quantity
                        else:  # short
                            pnl = (entry_price - exit_price) * quantity
                    else:
                        print(f"[WARN] Missing prices for PnL calculation between open trade {open_trade.get('order_id')} and exit trade {exit_trade.get('order_id')}")

                except Exception as e:
                    print(f"[ERROR] Exception during PnL calculation: {e}")
                    pnl = 0.0

                # Set FIFO match info on exit trade
                exit_trade['fifo_match'] = "YES"
                exit_trade['fifo_match_order_id'] = open_trade.get('order_id', '')

                try:
                    open_trades_ref = firebase_db.reference(f"/open_active_trades/{open_trade['symbol']}")
                    commissions = 7.02
                    net_pnl = pnl - commissions
                    reason_raw = (open_trade.get("exit_reason_raw") or "").upper()
                    friendly = REASON_MAP.get(reason_raw, "FILLED")

                    # Update Firebase to reflect trade exit and realized PnL on the open trade
                    open_trades_ref.child(open_trade['order_id']).update({
                        "exited": True,
                        "trade_state": "closed",
                        "contracts_remaining": 0,
                        "realized_pnl": pnl,
                        "tiger_commissions": commissions,
                        "net_pnl": net_pnl,
                        "exit_timestamp": datetime.utcnow().isoformat() + "Z",
                        "exit_reason": friendly_reason
                    })

                    # Update Firebase for exit trade with FIFO info
                    open_trades_ref.child(exit_trade['order_id']).update({
                        "fifo_match": "YES",
                        "fifo_match_order_id": open_trade.get('order_id', '')
                    })

                    print(f"[INFO] FIFO matched exit trade {exit_trade.get('order_id')} to open trade {open_trade.get('order_id')} with realized PnL {pnl:.2f} and updated Firebase")
                except Exception as e:
                    print(f"❌ Failed to update Firebase for trade {open_trade.get('order_id')}: {e}")

                matched = True
                break
        if not matched:
            print(f"[WARN] No matching open trade found for exit trade {exit_trade.get('order_id')}")

    return active_trades
# ========================================================
# MONITOR TRADES LOOP - CENTRAL LOOP 
# ========================================================

def monitor_trades():
    exit_in_progress = set()
    print(f"[DEBUG] Starting monitor_trades loop at {datetime.now(NZ_TZ)}")
    symbol = firebase_active_contract.get_active_contract()
    if not symbol:
        print("❌ No active contract symbol found in Firebase; aborting monitor_trades")
        return

    print("DO WE GET TO HERE????")
    # Load trailing TP settings
    trigger_points, offset_points = load_trailing_tp_settings()

    # Heartbeat logging every 60 seconds
    current_time = time.time()
    if not hasattr(monitor_trades, 'last_heartbeat'):
        monitor_trades.last_heartbeat = 0
    if current_time - monitor_trades.last_heartbeat >= 60:
        active_symbol = firebase_active_contract.get_active_contract()
        if not active_symbol:
            print("❌ No active contract symbol for live price fetch")
            mgc_price = None
        else:
            mgc_price = load_live_prices().get(active_symbol, {}).get('price')
        print(f"🛰️ System working – {active_symbol} price: {mgc_price}")
        monitor_trades.last_heartbeat = current_time
    
    # Load open trades from Firebase
    all_trades = load_open_trades(symbol)

    live_pos_data = firebase_db.reference("/live_total_positions").get() or {}
    position_count = live_pos_data.get("position_count", 0)

    run_zombie_cleanup_if_ready(all_trades, firebase_db, position_count, grace_period_seconds=20)

    print(f"ALL TRADES TO PROCESS ARE: {all_trades}")
    # Filter active trades
    active_trades = []
    GHOST_STATUSES = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
    existing_zombies = set(firebase_db.reference("/zombie_trades_log").get() or {})
    existing_ghosts = set(firebase_db.reference("/ghost_trades_log").get() or {})

    for t in all_trades:
        print("PRINTOUT OUT TRADE")
        print(t)
        print("CLOSING PRINT OUT TRADE")
        order_id = t.get('order_id')
        # Skip no order id
        if not order_id:
            print("⚠️ Skipping trade with no order_id")
            continue
        # Skip archived trades
        if is_archived_trade(order_id, firebase_db):
            print(f"⏭️ Skipping archived trade {order_id}")
            continue
        # Skip Zombie Trades in Zombie Log
        if order_id in existing_zombies:
            print(f"⏭️ Skipping zombie trade {order_id}")
            continue
        # Skip Ghost Trades in Ghost Log
        if order_id in existing_ghosts:
            print(f"⏭️ Skipping ghost trade {order_id}")
            continue
        # Skip exited/closed trades
        if t.get('exited') or t.get('status') in ['failed', 'closed']:
            print(f"🔁 Skipping exited/closed trade {order_id}")
            continue
        # Skip trades that are not filled or have no contracts remaining unless they are ghost trades
        if not t.get('filled') and t.get('status', '').upper() not in GHOST_STATUSES:
            print(f"🧾 Skipping {order_id} ⚠️ not filled and not a ghost trade")
            continue
        status = t.get('status', '').upper()
        # Skip trades with no trigger points or offset points
        if trigger_points < 0.01 or offset_points < 0.01:
            print(f"⚠️ Skipping trade {order_id} due to invalid TP config: trigger={trigger_points}, buffer={offset_points}")
            continue
        active_trades.append(t)

    if not active_trades:
        print("⚠️ No active trades found — Trade Worker happy & awake.")

    # Load live prices
    prices = load_live_prices()

    # Run trailing TP and exit processing
    print(f"[DEBUG] Processing {len(active_trades)} active trades for trailing TP and exits")
    active_trades = process_trailing_tp_and_exits(active_trades, prices, trigger_points, offset_points, exit_in_progress)

    # FIFO matching and flattening
    fifo_match_and_flatten(active_trades, symbol, firebase_db)

    # Archive and delete matched trades
    matched_trades = [t for t in active_trades if t.get('exited') or t.get('trade_state') == 'closed']
    archive_and_delete_matched_trades(symbol, matched_trades)

    # Remove matched trades from active list
    active_trades = [t for t in active_trades if t not in matched_trades]

    # Save remaining active trades
    # Filter out zero-contract trades before saving
    active_trades = [t for t in active_trades if t.get('contracts_remaining', 0) > 0]
    save_open_trades(symbol, active_trades)
    print(f"[DEBUG] Saved {len(active_trades)} active trades after processing")

    ##========END OF MAIN MONITOR TRADES LOOP FUNCTION========##
    
    print(f"[DEBUG] monitor_trades loop completed at {datetime.now(NZ_TZ)}")
    print(f"[DEBUG] Remaining active trades: {len(active_trades)}")

# ============================================================================
# 🟩 GREEN PATCH: Invert Grace Period Logic for Stable Zero Position Detection
# ============================================================================

zombie_first_seen = {}

def run_zombie_cleanup_if_ready(trades_list, firebase_db, position_count, grace_period_seconds=20):
    now = time.time()
    open_trades_ref = firebase_db.reference("/open_active_trades")
    zombie_trades_ref = firebase_db.reference("/zombie_trades_log")

    for trade in trades_list:
        order_id = trade.get("order_id")
        symbol = trade.get("symbol", "UNKNOWN")

        # Check global position count outside or passed in separately
        # Assuming you have a variable position_count elsewhere

        if position_count == 0:
            if order_id not in zombie_first_seen:
                zombie_first_seen[order_id] = now
                print(f"⏳ Started timer for trade {order_id} on {symbol} due to zero global position")
                continue

            elapsed = now - zombie_first_seen[order_id]
            if elapsed >= grace_period_seconds:
                print(f"🧟 Archiving trade {order_id} on {symbol} as zombie after {elapsed:.1f}s global zero position")
                trade['contracts_remaining'] = 0
                trade['trade_state'] = 'closed'
                trade['is_open'] = False

                try:
                    zombie_trades_ref.child(order_id).set(trade)
                    open_trades_ref.child(symbol).child(order_id).delete()
                    print(f"🗑️ Deleted trade {order_id} from open_active_trades")
                except Exception as e:
                    print(f"❌ Failed to archive/delete trade {order_id}: {e}")

                zombie_first_seen.pop(order_id, None)
        else:
            if order_id in zombie_first_seen:
                print(f"✅ Trade {order_id} on {symbol} no longer zero global position; clearing timer")
                zombie_first_seen.pop(order_id)

if __name__ == '__main__':
    while True:
        try:
            monitor_trades()
        except Exception as e:
            print(f"❌ ERROR in monitor_trades(): {e}")
        time.sleep(10)

    # =========================  END OF SCRIPT ================================
