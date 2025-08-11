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
    matching_trade_id = None
    for trade_id, trade in open_trades.items():
        if trade.get("exit_order_id") == exit_order_id:
            matching_trade_id = trade_id
            print(f"[DEBUG] Found matching trade {trade_id} for exit_order_id {exit_order_id}")
            break

    if not matching_trade_id:
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
            print(f"[WARN] Missing entry price or quantity for trade {matching_trade_id}; skipping P&L calculation.")
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
            print(f"[INFO] Calculated realized P&L for trade {matching_trade_id}: {pnl:.2f}")
    except Exception as e:
        print(f"[ERROR] Exception during P&L calculation for trade {matching_trade_id}: {e}")

    trade_ref = open_active_trades_ref.child(matching_trade_id)
    print(f"[DEBUG] Updating fill details and P&L for trade {matching_trade_id}")

    try:
        trade_ref.update(update_data)
        print(f"[INFO] Updated trade {matching_trade_id} with fill data and P&L in Firebase")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to update trade {matching_trade_id}: {e}")
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

def is_archived_trade(trade_id, firebase_db):
    archived_ref = firebase_db.reference("/archived_trades_log")
    archived_trades = archived_ref.get() or {}
    return trade_id in archived_trades

#=============================================
# 🟢  HELPER: to Archive trade before deletion
#=============================================

def archive_trade(symbol, trade):
    trade_id = trade.get("trade_id")
    if not trade_id:
        print(f"❌ Cannot archive trade without trade_id")
        return False
    try:
        archive_ref = db.reference(f"/archived_trades_log/{trade_id}")
        if "trade_type" not in trade or not trade["trade_type"]:
            trade["trade_type"] = "UNKNOWN"
        archive_ref.set(trade)
        print(f"[DEBUG] Archiving trade {trade_id} with trade_type: {trade.get('trade_type')}")
        return True
    except Exception as e:
        print(f"❌ Failed to archive trade {trade_id}: {e}")
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
        trade_id = trade.get("trade_id")
        if not trade_id:
            print("⚠️ Skipping trade with no trade_id during archive/delete")
            continue

        # Archive the trade first
        success = archive_trade(symbol, trade)
        if not success:
            print(f"❌ Failed to archive trade {trade_id}; skipping deletion")
            continue

        # Delete from open trades
        try:
            open_trades_ref.child(trade_id).delete()
            print(f"✅ Archived and deleted trade {trade_id} for symbol {symbol}")
        except Exception as e:
            print(f"❌ Failed to delete trade {trade_id} from Firebase: {e}")

#=============================
# Firebase open trades handler
#=============================

def load_open_trades(symbol):
    try:
        ref = db.reference(f"/open_active_trades/{symbol}")
        data = ref.get() or {}
        trades = []
        if isinstance(data, dict):
            for tid, td in data.items():
                td['trade_id'] = tid
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
            trade_id = t.get("trade_id")
            if not trade_id:
                continue
            ref.child(trade_id).update(t)
            print(f"✅ Open Active Trade {trade_id} saved to Firebase.")
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
            print(f"🔒 Skipping closed trade {trade.get('trade_id')}")
            continue
        trade_id = trade.get('trade_id', 'unknown')
        print(f"🔄 Processing trade {trade_id}")
        if trade.get('exited') or trade_id in exit_in_progress:
            continue

        symbol = trade['symbol']
        direction = 1 if trade['action'] == 'BUY' else -1
        current_price = prices.get(symbol, {}).get('price') if isinstance(prices.get(symbol), dict) else prices.get(symbol)

        if current_price is None:
            print(f"⚠️ No price for {symbol} — skipping {trade_id}")
            continue

        entry = trade.get('filled_price')
        if entry is None:
            print(f"❌ Trade {trade_id} missing filled_price, skipping.")
            continue

        if not trade.get('trail_hit'):
            trade['trail_peak'] = entry
            trigger_price = entry + trigger_points if direction == 1 else entry - trigger_points
            print(f"[DEBUG] TP trigger price for trade {trade_id} set at {trigger_price:.2f}")
            if (direction == 1 and current_price >= trigger_price) or (direction == -1 and current_price <= trigger_price):
                trade['trail_hit'] = True
                trade['trail_peak'] = current_price
                print(f"[INFO] TP trigger HIT for trade {trade_id} at price {current_price:.2f}")

                try:
                    open_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
                    open_trades_ref.child(trade_id).update({
                        "trail_hit": True,
                        "trail_peak": current_price
                    })
                except Exception as e:
                    print(f"❌ Failed to update trail_hit in Firebase for trade {trade_id}: {e}")

        if trade.get('trail_hit'):
            if (direction == 1 and current_price > trade['trail_peak']) or (direction == -1 and current_price < trade['trail_peak']):
                trade['trail_peak'] = current_price
            buffer_amt = offset_points
            if (direction == 1 and current_price <= trade['trail_peak'] - buffer_amt) or (direction == -1 and current_price >= trade['trail_peak'] + buffer_amt):
                print(f"🚨 Trailing TP exit for {trade_id}: price={current_price}, peak={trade['trail_peak']}")
                print(f"[INFO] Trailing TP EXIT triggered for trade {trade_id}: current price={current_price:.2f}, trail peak={trade['trail_peak']:.2f}, buffer={buffer_amt}")

                if trade.get("exit_in_progress", False):
                    print(f"⏭️ Exit order already in progress for trade {trade_id}, skipping new exit order.")
                    continue

                try:
                    result = place_exit_trade(symbol, 'SELL' if trade['action'] == 'BUY' else 'BUY', 1, firebase_db)

                    if result.get("status") == "SUCCESS":
                        print(f"📤 Exit order placed successfully for trade {trade_id}")

                        # Update Firebase with exit fill details
                        update_trade_on_exit_fill(firebase_db, symbol, result["order_id"], result["action"], result["quantity"], result.get("filled_price"), result.get("transaction_time"))

                        # Patch: Update trade_type in Firebase for this trade
                        try:
                            open_trades_ref.child(trade_id).update({
                                "trade_type": result.get("trade_type", "")
                            })
                            print(f"✅ Updated trade_type to {result.get('trade_type')} for trade {trade_id}")
                        except Exception as e:
                            print(f"❌ Failed to update trade_type for trade {trade_id}: {e}")

                        exit_in_progress.add(trade_id)
                        trade['exit_in_progress'] = True
                        open_trades_ref.child(trade_id).update({
                            "exit_in_progress": True,
                            "exit_order_id": result["order_id"]
                        })
                    else:
                        print(f"❌ Exit order failed for trade {trade_id}: {result}")

                        print(f"📤 Exit order placed successfully for trade {trade_id}: {result}")
                except Exception as e:
                    print(f"❌ Exception placing exit trade for trade {trade_id}: {e}")

        # Update the trade back in active_trades list (in-place)
        active_trades[i] = trade

    return active_trades

# ==============================================
# 🟩 FIFO MATCH AND FLATTEN WITH FIREBASE UPDATE
# ==============================================

def fifo_match_and_flatten(active_trades, symbol):
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
    open_trades.sort(key=lambda t: t.get('entry_timestamp', t['trade_id']))

    print(f"[DEBUG] Found {len(exit_trades)} exit trades and {len(open_trades)} open trades for matching")

    for exit_trade in exit_trades:
        matched = False
        for open_trade in open_trades:
            if open_trade.get('action') != exit_trade.get('exit_action') and not open_trade.get('exited'):
                open_trade['exited'] = True
                open_trade['trade_state'] = 'closed'
                open_trade['contracts_remaining'] = 0

                # Calculate PnL here
                try:
                    entry_price = open_trade.get("filled_price")
                    exit_price = exit_trade.get("filled_price")  # or exit_fill_price if set elsewhere
                    quantity = exit_trade.get("exit_filled_qty", 1)  # fallback to 1
                    
                    pnl = 0.0
                    if entry_price is not None and exit_price is not None:
                        if open_trade.get('action') == 'BUY':
                            pnl = (exit_price - entry_price) * quantity
                        else:  # short
                            pnl = (entry_price - exit_price) * quantity
                    else:
                        print(f"[WARN] Missing prices for PnL calculation between open trade {open_trade.get('trade_id')} and exit trade {exit_trade.get('trade_id')}")

                except Exception as e:
                    print(f"[ERROR] Exception during PnL calculation: {e}")
                    pnl = 0.0

                # Set FIFO match info on exit trade
                exit_trade['fifo_match'] = "YES"
                exit_trade['fifo_match_order_id'] = open_trade.get('trade_id', '')

                try:
                    open_trades_ref = firebase_db.reference(f"/open_active_trades/{open_trade['symbol']}")
                    commissions = 7.02  
                    net_pnl = pnl - commissions

                    # Update Firebase to reflect trade exit and realized PnL on the open trade
                    open_trades_ref.child(open_trade['trade_id']).update({
                        "exited": True,
                        "trade_state": "closed",
                        "contracts_remaining": 0,
                        "realized_pnl": pnl,
                        "tiger_commissions": commissions,
                        "net_pnl": net_pnl
                    })

                    # Update Firebase for exit trade with FIFO info
                    open_trades_ref.child(exit_trade['trade_id']).update({
                        "fifo_match": "YES",
                        "fifo_match_order_id": open_trade.get('trade_id', '')
                    })

                    print(f"[INFO] FIFO matched exit trade {exit_trade.get('trade_id')} to open trade {open_trade.get('trade_id')} with realized PnL {pnl:.2f} and updated Firebase")
                except Exception as e:
                    print(f"❌ Failed to update Firebase for trade {open_trade.get('trade_id')}: {e}")

                matched = True
                break
        if not matched:
            print(f"[WARN] No matching open trade found for exit trade {exit_trade.get('trade_id')}")

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

    # Run zombie cleanup if live positions are zero and stale enough
    run_zombie_cleanup_if_ready(db)

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
        tid = t.get('trade_id')
        # Skip no trade id
        if not tid:
            print("⚠️ Skipping trade with no trade_id")
            continue
        # Skip archived trades
        if is_archived_trade(tid, firebase_db):
            print(f"⏭️ Skipping archived trade {tid}")
            continue
         # Skip Zombie Trades in Zombie Log
        if tid in existing_zombies:
            print(f"⏭️ Skipping zombie trade {tid}")
            continue
        # Skip Ghost Trades in Ghost Log
        if tid in existing_ghosts:
            print(f"⏭️ Skipping ghost trade {tid}")
            continue
        # Skip exited/closed trades
        if t.get('exited') or t.get('status') in ['failed', 'closed']:
            print(f"🔁 Skipping exited/closed trade {tid}")
            continue
        # Skip trades that are not filled or have no contracts remaining unless they are ghost trades
        if not t.get('filled') and t.get('status', '').upper() not in GHOST_STATUSES:
           print(f"🧾 Skipping {tid} ⚠️ not filled and not a ghost trade")
           continue
        status = t.get('status', '').upper()
        # Skip trades with no trigger points or offset points
        if trigger_points < 0.01 or offset_points < 0.01:
            print(f"⚠️ Skipping trade {tid} due to invalid TP config: trigger={trigger_points}, buffer={offset_points}")
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
    fifo_match_and_flatten(active_trades, symbol)

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

def run_zombie_cleanup_if_ready(firebase_db, grace_period_seconds=20):
    global last_cleanup_timestamp

    live_ref = firebase_db.reference("/live_total_positions")
    data = live_ref.get() or {}

    position_count = data.get("position_count")
    last_updated_raw = data.get("last_updated")

    if position_count is None or last_updated_raw is None:
        print("⚠️ /live_total_positions data missing")
        return

    try:
        position_count_val = float(position_count)
    except Exception:
        print(f"⚠️ Invalid position_count: {position_count}")
        return

    # Parse last_updated timestamp, robustly
    try:
        if isinstance(last_updated_raw, str):
            last_updated = datetime.fromisoformat(last_updated_raw.replace("Z", "+00:00"))
        elif isinstance(last_updated_raw, (int, float)):
            last_updated = datetime.utcfromtimestamp(last_updated_raw / 1000)
        else:
            last_updated = last_updated_raw
    except Exception as e:
        print(f"⚠️ Failed parsing last_updated: {e}")
        return

    now_utc = datetime.utcnow()
    delta_seconds = (now_utc - last_updated).total_seconds()

    print(f"[INFO] Zombie cleanup check - pos count: {position_count_val}, data age: {delta_seconds:.1f}s")

    # No skipping — run every loop when pos=0 and grace period passed
    if position_count_val != 0:
        print("[INFO] Positions open; skipping zombie cleanup.")
        return

    if delta_seconds < grace_period_seconds:
        print(f"[INFO] Position count zero but data only {delta_seconds:.1f}s old; skipping zombie cleanup.")
        return

    print("[INFO] Running zombie cleanup now...")

    open_trades_ref = firebase_db.reference("/open_active_trades")
    zombie_trades_ref = firebase_db.reference("/zombie_trades_log")
    all_open_trades = open_trades_ref.get() or {}

    if not isinstance(all_open_trades, dict):
        print("⚠️ open_active_trades not dict; skipping")
        return

    for symbol, trades_by_id in all_open_trades.items():
        if not isinstance(trades_by_id, dict):
            print(f"⚠️ Skipping {symbol}, not dict")
            continue

        for trade_id, trade in trades_by_id.items():
            if not isinstance(trade, dict):
                continue

            try:
                if trade.get('contracts_remaining', 0) <= 0:
                    print(f"🧟 Archiving zero-contract trade {trade_id} for {symbol} as zombie")
                    trade['contracts_remaining'] = 0
                    trade['trade_state'] = 'closed'
                    trade['is_open'] = False

                    zombie_trades_ref.child(trade_id).set(trade)
                    open_trades_ref.child(symbol).child(trade_id).delete()
                    print(f"🗑️ Deleted zero-contract trade {trade_id} from open_active_trades")
            except Exception as e:
                print(f"❌ Failed to archive/delete trade {trade_id}: {e}")

if __name__ == '__main__':
    while True:
        try:
            monitor_trades()
        except Exception as e:
            print(f"❌ ERROR in monitor_trades(): {e}")
        time.sleep(10)

    # =========================  END OF SCRIPT ================================
