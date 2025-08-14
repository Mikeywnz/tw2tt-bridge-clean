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
import pprint

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
# 🟢 TRAILING TP & EXIT (minimal, FIFO-first; uses handle_exit_fill_from_tx)
# ==========================================================
# ==========================================================
# 🟩 TRAILING TP AND EXIT PROCESSING WITH place_exit_trade()
# ==========================================================
def process_trailing_tp_and_exits(active_trades, prices, trigger_points, offset_points):
    closed_ids = set()
    print(f"[DEBUG] process_trailing_tp_and_exits() called with {len(active_trades)} active trades")

    for i, trade in enumerate(active_trades):
        if not trade or not isinstance(trade, dict):
            continue
        if trade.get("status") == "closed":
            print(f"🔒 Skipping closed trade {trade.get('order_id')}")
            continue

        order_id = trade.get('order_id', 'unknown')
        symbol = trade.get('symbol')
        print(f"🔄 Processing trade {order_id}")

        direction = 1 if trade.get('action') == 'BUY' else -1
        current_price = prices.get(symbol, {}).get('price') if isinstance(prices.get(symbol), dict) else prices.get(symbol)
        if current_price is None:
            print(f"⚠️ No price for {symbol} — skipping {order_id}")
            continue

        entry = trade.get('filled_price')
        if entry is None:
            print(f"❌ Trade {order_id} missing filled_price, skipping.")
            continue

        # ---- Trigger arming ----
        if not trade.get('trail_hit'):
            trade['trail_peak'] = entry
            trigger_price = entry + trigger_points if direction == 1 else entry - trigger_points
            print(f"[DEBUG] {order_id} trigger @ {trigger_price:.2f} (entry {entry:.2f}, dir {'LONG' if direction==1 else 'SHORT'})")
            if (direction == 1 and current_price >= trigger_price) or (direction == -1 and current_price <= trigger_price):
                trade['trail_hit'] = True
                trade['trail_peak'] = current_price
                print(f"[INFO] TP trigger HIT for {order_id} at {current_price:.2f}")
                try:
                    open_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
                    open_trades_ref.child(order_id).update({"trail_hit": True, "trail_peak": current_price})
                except Exception as e:
                    print(f"❌ Failed to update trail_hit for {order_id}: {e}")

        # ---- Trail peak monotonic update + exit check ----
        if trade.get('trail_hit'):
            prev_peak = trade.get('trail_peak', entry)
            new_peak = max(prev_peak, current_price) if direction == 1 else min(prev_peak, current_price)

            if new_peak != prev_peak:
                print(f"[DEBUG] New trail peak for {order_id}: {new_peak:.2f} (prev: {prev_peak:.2f})")
                trade['trail_peak'] = new_peak
                try:
                    firebase_db.reference(f"/open_active_trades/{symbol}").child(order_id).update({"trail_peak": new_peak})
                except Exception as e:
                    print(f"❌ Failed to update trail_peak for {order_id}: {e}")
            else:
                print(f"[DEBUG] Trail peak unchanged for {order_id}: {prev_peak:.2f}")

            buffer_amt = float(offset_points)
            print(f"[DEBUG] Buffer for {order_id}: {buffer_amt:.2f} | price {current_price:.2f} vs peak {trade['trail_peak']:.2f}")

            exit_trigger = (
                (direction == 1 and current_price <= trade['trail_peak'] - buffer_amt) or
                (direction == -1 and current_price >= trade['trail_peak'] + buffer_amt)
            )

            if exit_trigger:
                print(f"[INFO] Trailing TP EXIT condition met for {order_id}")
                try:
                    exit_side = 'SELL' if trade.get('action') == 'BUY' else 'BUY'
                    result = place_exit_trade(symbol, exit_side, 1, firebase_db)
                    if result.get("status") == "SUCCESS":
                        print(f"📤 Exit order placed successfully for {order_id}")

                        # Build tx_dict exactly like execute_trades_live returns
                        tx_dict = {
                            "status": result.get("status", "SUCCESS"),
                            "order_id": str(result.get("order_id", "")),
                            "trade_type": result.get("trade_type", "EXIT"),
                            "symbol": symbol,
                            "action": result.get("action", exit_side),
                            "quantity": result.get("quantity", 1),
                            "filled_price": result.get("filled_price"),
                            "transaction_time": result.get("transaction_time"),
                        }
                        
                        # Hand off → close FIFO anchor, returns closed anchor_id
                        anchor_id = handle_exit_fill_from_tx(firebase_db, tx_dict)
                        if anchor_id:
                            closed_ids.add(anchor_id)
                            if order_id == anchor_id:
                                trade['exited'] = True
                                trade['trade_state'] = 'closed'
                                trade['contracts_remaining'] = 0
                            print(f"[INFO] Exit ticket processed for {anchor_id} → {tx_dict['order_id']}")
                        else:
                            print(f"⚠️ Exit handler did not return anchor id for {order_id}")
                    else:
                        print(f"❌ Exit order failed for {order_id}: {result}")
                except Exception as e:
                    print(f"❌ Exception placing exit for {order_id}: {e}")

        # Write back in-place
        active_trades[i] = trade

    return active_trades
# ==============================================
# 🟩 EXIT TICKET (tx_dict) → MINIMAL FIFO CLOSE
# ==============================================

def handle_exit_fill_from_tx(firebase_db, tx_dict):
    """
    tx_dict example (from execute_trades_live):
      {
        "status": "SUCCESS",
        "order_id": "40126…",
        "trade_type": "FLATTENING_SELL" | "FLATTENING_BUY" | "EXIT",
        "symbol": "MGC2510",
        "action": "SELL" | "BUY",
        "quantity": 1,
        "filled_price": 3374.3,
        "transaction_time": "2025-08-13T06:55:46Z"
      }
    """

    # 1) Extract + sanity
    exit_oid   = str(tx_dict.get("order_id", "")).strip()
    symbol     = tx_dict.get("symbol")
    exit_price = tx_dict.get("filled_price")
    exit_time  = tx_dict.get("transaction_time")
    exit_act   = (tx_dict.get("action") or "").upper()
    status     = tx_dict.get("status", "SUCCESS")
    qty_raw    = tx_dict.get("quantity") or tx_dict.get("filled_qty") or 1
    try:
        exit_qty = max(1, int(qty_raw))
    except Exception:
        exit_qty = 1

    if not (exit_oid and exit_oid.isdigit() and symbol and exit_price is not None):
        print(f"❌ Invalid exit payload: order_id={exit_oid}, symbol={symbol}, price={exit_price}")
        return False

    # 2) Log/Upsert exit ticket (never in open_active_trades)
    tickets_ref = firebase_db.reference(f"/exit_orders_log/{symbol}")
    tickets_ref.child(exit_oid).update({
        "order_id": exit_oid,
        "action": exit_act,
        "filled_price": exit_price,
        "filled_qty": exit_qty,
        "fill_time": exit_time,
        "status": status,
        "trade_type": tx_dict.get("trade_type", "EXIT")
    })
    print(f"[INFO] Exit ticket recorded: {exit_oid} @ {exit_price} ({exit_act})")

    # 3) Fetch oldest open anchor (FIFO by entry_timestamp)
    open_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
    opens = open_ref.get() or {}
    if not opens:
        print("[WARN] No open trades to close for this exit.")
        return False

    def _entry_ts(t): return t.get("entry_timestamp", "9999-12-31T23:59:59Z")
    candidates = [
        dict(tr, order_id=oid)
        for oid, tr in opens.items()
        if not tr.get("exited") and tr.get("contracts_remaining", 1) > 0
    ]
    if not candidates:
        print("[WARN] No eligible open trades (all exited or zero qty).")
        return False

    anchor = min(candidates, key=_entry_ts)
    anchor_oid = anchor["order_id"]
    print(f"[INFO] FIFO anchor selected: {anchor_oid} (entry={anchor.get('entry_timestamp')})")

    # 4) Compute P&L (anchor entry vs exit fill)
    try:
        entry_price = float(anchor.get("filled_price"))
        px_exit     = float(exit_price)
        qty         = exit_qty
        if anchor.get("action", "").upper() == "BUY":
            pnl = (px_exit - entry_price) * qty
        else:  # short anchor
            pnl = (entry_price - px_exit) * qty
    except Exception as e:
        print(f"❌ PnL calc error for anchor {anchor_oid}: {e}")
        pnl = 0.0
    print(f"[INFO] P&L for {anchor_oid} via exit {exit_oid}: {pnl:.2f}")

    # 5) Close anchor (sticky entry_timestamp preserved)
    COMMISSION_FLAT = 7.02  # ✅ your confirmed flat fee
    update = {
        "exited": True,
        "trade_state": "closed",
        "contracts_remaining": 0,
        "exit_timestamp": exit_time,          # use transaction fill time
        "exit_reason": "FILLED",              # can be mapped later if you add raw reason
        "realized_pnl": pnl,
        "tiger_commissions": COMMISSION_FLAT,
        "net_pnl": pnl - COMMISSION_FLAT,
        "exit_order_id": exit_oid,
    }
    try:
        open_ref.child(anchor_oid).update(update)
        print(f"[INFO] Anchor {anchor_oid} marked closed.")
    except Exception as e:
        print(f"❌ Failed to update anchor {anchor_oid}: {e}")
        return False

       # 6) Archive & delete anchor
    try:
        archive_ref = firebase_db.reference(f"/archived_trades_log/{anchor_oid}")
        archive_ref.set({**anchor, **update})
        open_ref.child(anchor_oid).delete()
        print(f"[INFO] Archived+deleted anchor {anchor_oid}")
    except Exception as e:
        print(f"❌ Archive/delete failed for {anchor_oid}: {e}")
        return None  # return None on failure

    # 7) Keep ticket (audit only; ignored by open loop)
    print(f"[INFO] Exit ticket retained under /exit_orders_log/{symbol}/{exit_oid}")
    return anchor_oid  # ← tell caller which open trade was closed

# ========================================================
# MONITOR TRADES LOOP - CENTRAL LOOP 
# ========================================================

def monitor_trades():
    print("[DEBUG] - entering monitor_trades()")
    symbol = firebase_active_contract.get_active_contract()
    if not symbol:
        print("❌ No active contract symbol found in Firebase; aborting monitor_trades")
        return

    # Load trailing TP settings
    trigger_points, offset_points = load_trailing_tp_settings()

    # Heartbeat (60s)
    now = time.time()
    if not hasattr(monitor_trades, 'last_heartbeat'):
        monitor_trades.last_heartbeat = 0
    if now - monitor_trades.last_heartbeat >= 60:
        live = load_live_prices()
        mgc_price = (live.get(symbol) or {}).get('price')
        print(f"🛰️  System working – {symbol} price: {mgc_price}")
        monitor_trades.last_heartbeat = now

    # Load open trades
    all_trades = load_open_trades(symbol)

    # Position count + zombie cleanup
    live_pos_data = firebase_db.reference("/live_total_positions").get() or {}
    position_count = live_pos_data.get("position_count", 0)
    run_zombie_cleanup_if_ready(all_trades, firebase_db, position_count, grace_period_seconds=20)

    # Filter active trades
    active_trades = []
    GHOST_STATUSES = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
    existing_zombies = set(firebase_db.reference("/zombie_trades_log").get() or {})
    existing_ghosts  = set(firebase_db.reference("/ghost_trades_log").get() or {})

    for t in all_trades:
        order_id = t.get('order_id')
        if not order_id:
            print("⚠️ Skipping trade with no order_id")
            continue
        if is_archived_trade(order_id, firebase_db):
            print(f"⏭️ Skipping archived trade {order_id}")
            continue
        if order_id in existing_zombies:
            print(f"⏭️ Skipping zombie trade {order_id}")
            continue
        if order_id in existing_ghosts:
            print(f"⏭️ Skipping ghost trade {order_id}")
            continue
        if t.get('exited') or t.get('status') in ['failed', 'closed']:
            print(f"🔁 Skipping exited/closed trade {order_id}")
            continue
        if not t.get('filled') and (t.get('status', '').upper() not in GHOST_STATUSES):
            print(f"🧾 Skipping {order_id} ⚠️ not filled and not a ghost trade")
            continue
        if trigger_points < 0.01 or offset_points < 0.01:
            print(f"⚠️ Skipping trade {order_id} due to invalid TP config: trigger={trigger_points}, buffer={offset_points}")
            continue
        active_trades.append(t)

    if not active_trades:
        print("⚠️ No active trades found — Trade Worker happy & awake.")

    # Prices (single fetch per loop)
    prices = load_live_prices()

    # Trailing TP & exit placement
    print(f"[DEBUG] Processing {len(active_trades)} active trades for trailing TP and exits")
    try:
        active_trades = process_trailing_tp_and_exits(active_trades, prices, trigger_points, offset_points)
    except Exception as e:
        print(f"❌ process_trailing_tp_and_exits error: {e}")

    # 3A: Reload open trades from Firebase to get latest state after TP exits
    all_trades = load_open_trades(symbol)
    active_trades = [t for t in all_trades if t.get('contracts_remaining', 0) > 0]

    # 🔽 EXIT LOGIC: drain exit tickets (placed by app.py or TP logic) and run FIFO close
    try:
        tickets_ref = firebase_db.reference(f"/exit_orders_log/{symbol}")
        tickets = tickets_ref.get() or {}
        # drain exit tickets
        for tx_id, tx in tickets.items():
            if not isinstance(tx, dict):
                continue
            if tx.get("_processed"):
                continue
            # Ensure symbol present (older tickets may miss it)
            if not tx.get("symbol"):
                tx["symbol"] = symbol
            ok = handle_exit_fill_from_tx(firebase_db, tx)
            if ok:
                tickets_ref.child(tx_id).update({"_processed": True})
                print(f"[INFO] Exit ticket {tx_id} processed and marked _processed")
    except Exception as e:
        print(f"❌ Exit ticket drain error: {e}")

    # 3B: Remove any trades from Firebase that were closed by exit tickets
    open_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
    for t in list(active_trades):
        if t.get('exited') or t.get('contracts_remaining', 0) <= 0:
            oid = t.get('order_id')
            if not oid:
                continue
            try:
                open_trades_ref.child(oid).delete()
                print(f"🗑️ Removed closed trade {oid} from Firebase after exit match")
            except Exception as e:
                print(f"⚠️ Failed to delete {oid} from Firebase: {e}")

    # Save remaining active trades (filter zero-qty)
    active_trades = [t for t in active_trades if t.get('contracts_remaining', 0) > 0]
    save_open_trades(symbol, active_trades)
    print(f"[DEBUG] Saved {len(active_trades)} active trades after processing")

    ##========END OF MAIN MONITOR TRADES LOOP FUNCTION========##

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
