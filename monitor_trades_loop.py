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

NZ_TZ = timezone(timedelta(hours=12))

firebase_db = db

# Trade fields usage:
# - trade_type: LONG_ENTRY, SHORT_ENTRY, FLATTENING_BUY, FLATTENING_SELL, etc. (Classification of trade)
# - status: FILLED, CANCELLED, EXPIRED, CLOSED, etc. (Order execution status)
# - trade_state: "open" or "closed" (Used for filtering trades in Firebase)
#Important: Do NOT set trade_type to "closed". Use 'status' or 'trade_state' to indicate closure.

#=========================
# FIREBASE INITIALIZATION 
#=========================

# Load Firebase secret key
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
cred = credentials.Certificate(firebase_key_path)

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred, {
        'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
    })

# === Helper Load live prices from Firebase ===
def load_live_prices():
    return db.reference("live_prices").get() or {}

# =====================================================================
# 🟩 HELPER UPDATED close_position() TO USE place_exit_trade() DIRECTLY
# =====================================================================

def close_position(symbol, original_action):
    print(f"[DEBUG] close_position() called with original_action='{original_action}'")
    exit_action = "SELL" if original_action == "BUY" else "BUY"
    print(f"[DEBUG] close_position() using exit_action='{exit_action}'")
    try:
        result = place_exit_trade(symbol, exit_action, 1, firebase_db)
        print(f"[DEBUG] place_exit_trade result: {result}")
        if result.get("status") != "SUCCESS":
            print(f"❌ Exit order failed: {result}")
        else:
            print(f"📤 Exit order placed successfully: {exit_action} 1 {symbol}")
    except Exception as e:
        print(f"❌ Failed to execute exit order: {e}")

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
    # Example: Fetch settings from Firebase or return defaults
    try:
        fb_url = f"{FIREBASE_URL}/trailing_tp_settings.json"
        res = requests.get(fb_url)
        cfg = res.json() if res.ok else {}
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
    firebase_url = f"https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/open_active_trades/{symbol}.json"
    try:
        resp = requests.get(firebase_url)
        resp.raise_for_status()
        data = resp.json() or {}
        trades = []
        if isinstance(data, dict):
            for tid, td in data.items():
                td['trade_id'] = tid
                trades.append(td)
        else:
            trades = []

        print(f"🔄 Loaded {len(trades)} open trades from Firebase.")
        return trades
    except Exception as e:
        print(f"❌ Failed to fetch open trades: {e}")
        return []

def save_open_trades(symbol, trades):
    try:
        for t in trades:
            trade_id = t.get("trade_id")
            if not trade_id:
                continue
            firebase_url = f"https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/open_active_trades/{symbol}/{trade_id}.json"
            requests.put(firebase_url, json=t).raise_for_status()
            print(f"✅ Open Active Trade {trade_id} saved to Firebase.")
    except Exception as e:
        print(f"❌ Failed to save open trades to Firebase: {e}")

# ==========================================================
# 🟩 TRAILING TP AND EXIT PROCESSING WITH place_exit_trade()
# ==========================================================

def process_trailing_tp_and_exits(active_trades, prices, trigger_points, offset_points, exit_in_progress):
    updated_trades = []

    for trade in active_trades:
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
            updated_trades.append(trade)
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
                    open_trades_ref = firebase_db.reference("/open_active_trades")
                    open_trades_ref.child(symbol).child(trade_id).update({
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

                try:
                    result = place_exit_trade(symbol, 'SELL' if trade['action']=='BUY' else 'BUY', 1, firebase_db)
                    if result.get("status") == "SUCCESS":
                        print(f"📤 Exit order placed successfully for trade {trade_id}")
                        exit_in_progress.add(trade_id)
                        trade['exit_in_progress'] = True
                        open_trades_ref = firebase_db.reference("/open_active_trades")
                        open_trades_ref.child(symbol).child(trade_id).update({
                            "exit_in_progress": True
                        })
                    else:
                        print(f"❌ Exit order failed for trade {trade_id}: {result}")
                except Exception as e:
                    print(f"❌ Exception placing exit trade for {trade_id}: {e}")

                updated_trades.append(trade)
                continue

        print(f"📌 Keeping trade {trade_id} OPEN – trail_hit={trade.get('trail_hit')}, exited={trade.get('exited')}, status={trade.get('status')}")
        updated_trades.append(trade)

    return updated_trades

# ==============================================
# 🟩 FIFO MATCH AND FLATTEN WITH FIREBASE UPDATE
# ==============================================

def fifo_match_and_flatten(active_trades):
    print(f"[DEBUG] fifo_match_and_flatten() called with {len(active_trades)} active trades")

    exit_trades = [t for t in active_trades if t.get('exit_in_progress') and not t.get('exited')]
    open_trades = [t for t in active_trades if not t.get('exited') and not t.get('exit_in_progress')]

    print(f"[DEBUG] Found {len(exit_trades)} exit trades and {len(open_trades)} open trades for matching")

    for exit_trade in exit_trades:
        matched = False
        for open_trade in open_trades:
            if open_trade.get('action') != exit_trade.get('exit_action') and not open_trade.get('exited'):
                open_trade['exited'] = True
                open_trade['trade_state'] = 'closed'
                open_trade['contracts_remaining'] = 0

                # Update Firebase to reflect trade exit
                try:
                    open_trades_ref = firebase_db.reference(f"/open_active_trades/{open_trade['symbol']}")
                    open_trades_ref.child(open_trade['trade_id']).update({
                        "exited": True,
                        "trade_state": "closed",
                        "contracts_remaining": 0
                    })
                    print(f"[INFO] FIFO matched exit trade {exit_trade.get('trade_id')} to open trade {open_trade.get('trade_id')} and updated Firebase")
                except Exception as e:
                    print(f"❌ Failed to update Firebase for trade {open_trade.get('trade_id')}: {e}")

                matched = True
                break
        if not matched:
            print(f"[WARN] No matching open trade found for exit trade {exit_trade.get('trade_id')}")

# ========================================================
# MONITOR TRADES LOOP with FIFO Matching and Debug Logging
# ========================================================

def monitor_trades():
    exit_in_progress = set()
    print(f"[DEBUG] Starting monitor_trades loop at {datetime.now(NZ_TZ)}")

    # Check live positions freshness and handle zombies/ghosts
    if not check_live_positions_freshness(db, grace_period_seconds=GRACE_PERIOD_SECONDS):
        print("[DEBUG] Skipping zombie trade check due to stale data or non-zero positions")
    else:
        pass  # Ghost/zombie logic disabled for debugging
            #   print("[DEBUG] Passing zombie trade check, handling zombies")
            #   handle_zombie_and_ghost_trades(db)

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

    # Get active symbol and open trades
    symbol = firebase_active_contract.get_active_contract()
    if not symbol:
        print("❌ No active contract symbol found in Firebase; aborting monitor_trades")
        return
    all_trades = load_open_trades(symbol)

    # Filter active trades
    active_trades = []
    # GHOST_STATUSES = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}

    for t in all_trades:
        tid = t.get('trade_id')
        if not tid:
            print("⚠️ Skipping trade with no trade_id")
            continue
        if t.get('exited') or t.get('status') in ['failed', 'closed']:
            print(f"🔁 Skipping exited/closed trade {tid}")
            continue
        # if not t.get('filled') and t.get('status', '').upper() not in GHOST_STATUSES:
        #     print(f"🧾 Skipping {tid} ⚠️ not filled and not a ghost trade")
        #     continue
        status = t.get('status', '').upper()
        # if t.get('contracts_remaining', 0) <= 0 and status not in GHOST_STATUSES:
        #     print(f"🧾 Skipping {tid} ⚠️ no contracts remaining and not a ghost trade")
        #     continue
        if trigger_points < 0.01 or offset_points < 0.01:
            print(f"⚠️ Skipping trade {tid} due to invalid TP config: trigger={trigger_points}, buffer={offset_points}")
            continue
        active_trades.append(t)

    if not active_trades:
        print("⚠️ No active trades found — Trade Worker happy & awake.")

    # Load live prices
    prices = load_live_prices()

    # FIFO matching and flattening
    fifo_match_and_flatten(active_trades)

    # Archive and delete matched trades
    matched_trades = [t for t in active_trades if t.get('exited') or t.get('trade_state') == 'closed']
    archive_and_delete_matched_trades(symbol, matched_trades)

    # Remove matched trades from active list
    active_trades = [t for t in active_trades if t not in matched_trades]

    # Save remaining active trades
    save_open_trades(symbol, active_trades)

     # Inside monitor_trades(), after saving open trades:
    active_trades = process_trailing_tp_and_exits(active_trades, prices, trigger_points, offset_points, exit_in_progress)

    updated_trades = []

 # ========================= MONITOR_TRADES_LOOP - END OF SEGMENT 2 ================================

 # ========================= MONITOR_TRADES_LOOP - Segment 3 =======================================


# ============================================================================
# 🟩 GREEN PATCH: Invert Grace Period Logic for Stable Zero Position Detection
# ============================================================================

def check_live_positions_freshness(firebase_db, grace_period_seconds=140):
    live_ref = firebase_db.reference("/live_total_positions")
    data = live_ref.get() or {}

    position_count = data.get("position_count", None)
    last_updated_str = data.get("last_updated", None)

    if position_count is None or last_updated_str is None:
        print("⚠️ /live_total_positions data incomplete or missing")
        return False

    try:
        # Convert position_count to float explicitly to avoid type mismatch
        position_count_val = float(position_count)
    except Exception:
        print(f"⚠️ Invalid position_count value: {position_count}")
        return False

    try:
        # Parse last_updated using your existing timezone setup
        nz_tz = timezone(timedelta(hours=12))  # NZST fixed offset; adjust manually if needed
        last_updated_str = last_updated_str.replace(" NZST", "")
        last_updated = datetime.strptime(last_updated_str, "%Y-%m-%d %H:%M:%S")
        last_updated = last_updated.replace(tzinfo=nz_tz)
    except Exception as e:
        print(f"⚠️ Failed to parse last_updated timestamp: {e}")
        return False

    now_nz = datetime.now(NZ_TZ)
    delta_seconds = (now_nz - last_updated).total_seconds()

    print(f"[DEBUG] Current time: {datetime.now(timezone(timedelta(hours=12)))}")
    print(f"[DEBUG] /live_total_positions last_updated: {last_updated} (NZST)")
    print(f"[DEBUG] Data age (seconds): {delta_seconds:.1f}")
    print(f"[DEBUG] Position count (float): {position_count_val}")

    # Inverted grace period logic:
    if position_count_val == 0:
        if delta_seconds < grace_period_seconds:
            print(f"⚠️ Position count zero but data only {delta_seconds:.1f}s old, skipping zombie check")
            return False
        else:
            print(f"✅ Position count zero and data stale enough ({delta_seconds:.1f}s), safe to run zombie detection")
            return True
    else:
        print("⚠️ Position count non-zero, skipping zombie detection to avoid false positives")
        return False

 # ========================= MONITOR_TRADES_LOOP - END OF SEGMENT 3 ================================

 # ========================= MONITOR_TRADES_LOOP - Segment 4 =======================================

# ============================================================================================
# 🟩 GREEN PATCH START: Zombie & Ghost Trade Handler with 30s Grace Period (DISABLED FOR TEST)
# ============================================================================================

ZOMBIE_COOLDOWN_SECONDS = 30
GHOST_GRACE_PERIOD_SECONDS = 30
ZOMBIE_STATUSES = {"FILLED"}  # Legitimate filled trades with no position
GHOST_STATUSES = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
GRACE_PERIOD_SECONDS = 140 

def handle_zombie_and_ghost_trades(firebase_db):
    now_utc = datetime.now(timezone.utc)
    open_trades_ref = firebase_db.reference("/open_active_trades")
    zombie_trades_ref = firebase_db.reference("/zombie_trades_log")
   # ghost_trades_ref = firebase_db.reference("/ghost_trades_log")

    all_open_trades = open_trades_ref.get() or {}
    existing_zombies = set(zombie_trades_ref.get() or {})
    existing_ghosts = set(ghost_trades_ref.get() or {})

    NZ_TZ = timezone(timedelta(hours=12))  # NZST fixed offset, adjust if needed
    now_nz = datetime.now(NZ_TZ)

    try:
        position_count = int(firebase_db.reference("/live_total_positions/position_count").get())
    except Exception:
        position_count = 0

    if position_count > 0:
        print("[INFO] Positions open; skipping zombie and ghost cleanup.")
        return

    if not isinstance(all_open_trades, dict):
        print("⚠️ all_open_trades is not a dict, skipping trade processing")
        return

    for symbol, trades_by_id in all_open_trades.items():
        if not isinstance(trades_by_id, dict):
            print(f"⚠️ Skipping trades for symbol {symbol} because it's not a dict")
            continue

        for trade_id, trade in trades_by_id.items():
            if not isinstance(trade, dict):
                continue

            status = trade.get("status", "").upper()
            filled = trade.get("filled", 0)

            #if trade_id in existing_zombies or trade_id in existing_ghosts:
            #    continue

            # if status in GHOST_STATUSES and filled == 0:
            #     print(f"👻 Archiving ghost trade {trade_id} for symbol {symbol} (no timestamp needed)")
            #     ghost_trades_ref.child(trade_id).set(trade)
            #     open_trades_ref.child(symbol).child(trade_id).delete()
            #     print(f"🗑️ Deleted ghost trade {trade_id} from /open_active_trades/")
            #     continue

            entry_ts_str = trade.get("entry_timestamp")
            if not entry_ts_str:
                print(f"⚠️ No entry_timestamp for trade {trade_id}; skipping cooldown check")
                continue

            try:
                entry_ts = datetime.fromisoformat(entry_ts_str.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception as e:
                print(f"⚠️ Failed to parse entry_timestamp for {trade_id}: {e}; skipping cooldown check")
                continue

            age_seconds = (now_utc - entry_ts).total_seconds()

            # if status in ZOMBIE_STATUSES:
            #     if age_seconds < ZOMBIE_COOLDOWN_SECONDS:
            #         print(f"⏳ Zombie trade {trade_id} age {age_seconds:.1f}s < cooldown {ZOMBIE_COOLDOWN_SECONDS}s — skipping")
            #         continue
            #     print(f"🧟‍♂️ Archiving zombie trade {trade_id} for symbol {symbol} (age {age_seconds:.1f}s)")
            #     trade["symbol"] = symbol
            #     zombie_trades_ref.child(trade_id).set(trade)

            #     open_trades_ref.child(symbol).child(trade_id).delete()
            #     print(f"🗑️ Deleted zombie trade {trade_id} from /open_active_trades()")

# ========================================================
# 🟩 REFACTORED handle_exit_order() FOR EXITS ONLY
# ========================================================

def handle_exit_order(symbol, action, quantity):
    open_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
    open_trades = open_trades_ref.get() or {}

    matching_trade_id = None
    for tid, trade in open_trades.items():
        if trade.get("action") == action and not trade.get("exited") and not trade.get("exit_in_progress"):
            matching_trade_id = tid
            break

    if matching_trade_id:
        trade_data = open_trades[matching_trade_id]
        if trade_data.get("exit_in_progress"):
            print(f"⚠️ Exit already in progress for trade {matching_trade_id}, skipping exit order placement")
            return {"status": "skipped", "reason": "exit_in_progress"}

    result = place_exit_trade(symbol, action, quantity, firebase_db)
    print(f"[INFO] place_exit_trade() result: {result}")

    if isinstance(result, dict) and result.get("status") == "SUCCESS" and matching_trade_id:
        max_retries = 5
        retry_count = 0
        while retry_count < max_retries:
            try:
                open_trades_ref.child(matching_trade_id).update({"exit_in_progress": True})
                updated_trade = open_trades_ref.child(matching_trade_id).get()
                if updated_trade and updated_trade.get("exit_in_progress") == True:
                    print(f"🟢 Confirmed exit_in_progress=True for trade {matching_trade_id}")
                    break
            except Exception as e:
                print(f"⚠️ Retry {retry_count+1}: Failed to set exit_in_progress: {e}")
            retry_count += 1
            time.sleep(1)
        else:
            print(f"❌ Failed to confirm exit_in_progress flag for trade {matching_trade_id} after {max_retries} retries")

    # Update trade on exit fills (assumed helper exists)
    if action.upper() in ["BUY", "SELL"]:
        if "exit_order_id" in result and result["exit_order_id"]:
            exit_order_id = result["exit_order_id"]
            exit_action = action
            filled_qty = result.get("filled_quantity", 0)
            try:
                update_trade_on_exit_fill(firebase_db, symbol, exit_order_id, exit_action, filled_qty)
            except Exception as e:
                print(f"❌ Failed to update trade on exit fill: {e}")

    # Push new or updated trade to Firebase (assuming FIREBASE_URL & others are defined)
    if isinstance(result, dict) and result.get("status") == "SUCCESS":
        def is_valid_trade_id(tid):
            return isinstance(tid, str) and tid.isdigit()

        raw = result.get("order_id")
        if isinstance(raw, int):
            trade_id = str(raw)
        elif isinstance(raw, str):
            trade_id = raw
        else:
            trade_id = None

        if not trade_id or not is_valid_trade_id(trade_id):
            print(f"❌ Invalid trade_id detected: {trade_id}")
            return {"status": "error", "message": "Invalid trade_id from execute_trade_live"}

        status = result.get("trade_status", "UNKNOWN")
        filled = result.get("filled_quantity", 0)

        if is_archived_trade(trade_id, firebase_db):
            print(f"⏭️ Ignoring archived trade {trade_id} in exit order handling")
            return {"status": "skipped", "reason": "archived trade"}

        # Assuming is_zombie_trade() exists if needed
        if 'is_zombie_trade' in globals() and is_zombie_trade(trade_id, firebase_db):
            print(f"⏭️ Ignoring zombie trade {trade_id} in exit order handling")
            return {"status": "skipped", "reason": "zombie trade"}

        filled_price = result.get("filled_price") or 0.0
        # entry_timestamp assumed to be retrievable here, else pass as param
        entry_timestamp = datetime.utcnow().isoformat() + "Z"

        new_trade = {
            "trade_id": trade_id,
            "symbol": symbol,
            "filled_price": filled_price,
            "action": action,
            "trade_type": trade_type,
            "status": status,
            "contracts_remaining": 1,
            "trail_trigger": trigger_points,
            "trail_offset": offset_points,
            "trail_hit": False,
            "trail_peak": filled_price,
            "filled": True,
            "entry_timestamp": entry_timestamp,
            "trade_state": "open",
            "just_executed": True,
            "executed_timestamp": datetime.utcnow().isoformat() + "Z"
        }

        try:
            FIREBASE_URL = "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
            endpoint = f"{FIREBASE_URL}/open_active_trades/{symbol}/{trade_id}.json"
            print("🟢 [LOG] Pushing trade to Firebase with payload: " + json.dumps(new_trade))
            import requests
            put = requests.put(endpoint, json=new_trade)
            if put.status_code == 200:
                print(f"✅ Firebase open_active_trades updated at key: {trade_id}")
            else:
                print(f"❌ Firebase update failed: {put.text}")
        except Exception as e:
            print(f"❌ Failed to push trade to Firebase: {e}")

    else:
        print(f"[❌] Trade result error: {result}")
        return {"status": "error", "message": f"Trade result: {result}"}

    return result

if __name__ == '__main__':
    while True:
        try:
            monitor_trades()
        except Exception as e:
            print(f"❌ ERROR in monitor_trades(): {e}")
        time.sleep(10)

    # ========================= MONITOR_TRADES_LOOP - END OF SEGMENT 4 END OF SCRIPT ================================
     