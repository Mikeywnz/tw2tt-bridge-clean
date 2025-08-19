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
from fifo_close import handle_exit_fill_from_tx


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

from datetime import datetime, timezone, timedelta

def save_open_trades(symbol, trades, grace_seconds: int = 60):
    """
    Atomic overwrite of /open_active_trades/{symbol} with a short grace period:
    - Writes all valid 'trades' you pass in.
    - Keeps any *existing* Firebase trade for this symbol if its entry_timestamp is within the last `grace_seconds`.
    - Flushes old/zombie entries.
    """
    ref = db.reference(f"/open_active_trades/{symbol}")

    def _parse_iso_to_utc(iso_str: str) -> datetime:
        """Tolerant ISO parser -> aware UTC datetime."""
        s = (iso_str or "").strip().replace("T", " ").replace("Z", "")
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
        # treat naive as UTC
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

    try:
        # 1) Build fresh payload from provided trades
        fresh = {}
        for t in trades:
            if not isinstance(t, dict):
                continue
            oid = t.get("order_id")
            if not oid:
                continue
            if t.get("symbol") not in (None, "", symbol):  # drop mismatched symbols
                continue
            status = (t.get("status") or "").lower()
            if t.get("exited") or status in ("closed", "failed") or (t.get("contracts_remaining", 0) or 0) <= 0:
                continue
            fresh[oid] = t

        # 2) Load existing to apply short grace for very-new trades
        existing = ref.get() or {}
        now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
        cutoff = now_utc - timedelta(seconds=grace_seconds)

        # Keep any existing recent trade not present in `fresh`
        for oid, tr in existing.items():
            if oid in fresh:
                continue
            if not isinstance(tr, dict):
                continue
            if tr.get("symbol") not in (None, "", symbol):
                continue
            status = (tr.get("status") or "").lower()
            if tr.get("exited") or status in ("closed", "failed") or (tr.get("contracts_remaining", 0) or 0) <= 0:
                continue
            # Use entry_timestamp; fallback to transaction_time if needed
            ts = tr.get("entry_timestamp") or tr.get("transaction_time") or ""
            ts_utc = _parse_iso_to_utc(ts)
            if ts_utc >= cutoff:
                fresh[oid] = tr  # protect very recent trade

        # 3) Atomic overwrite with protected set
        ref.set(fresh)
        print(f"✅ Open Active Trades overwritten atomically (kept {len(fresh)}; grace={grace_seconds}s)")
    except Exception as e:
        print(f"❌ Failed to save open trades to Firebase: {e}")

# =========================================================================
# 🟢 TRAILING TP & EXIT (minimal, FIFO-first; uses handle_exit_fill_from_tx)
# ==========================================================================

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

                        # Enqueue the exit ticket; drain loop will process it exactly once
                        tickets_ref = firebase_db.reference("/exit_orders_log")
                        try:
                            tickets_ref.child(tx_dict["order_id"]).set({**tx_dict, "_processed": False})
                        except Exception as e2:
                            print(f"❌ Failed to enqueue exit ticket {tx_dict.get('order_id')}: {e2}")
                        else:
                            print(f"[INFO] Exit ticket enqueued (not processed here): {tx_dict['order_id']}")
                    else:
                        print(f"❌ Exit order failed for {order_id}: {result}")

                except Exception as e:
                    print(f"❌ Exception placing exit for {order_id}: {e}")

        # Write back in-place
        active_trades[i] = trade

    return active_trades

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

    # =========================
    # 🟩 ANCHOR GATE – DROP-IN
    # =========================
    def _iso_to_utc(s: str):
        try:
            s = (s or "").strip().replace("T", " ").replace("Z", "")
            dt = datetime.fromisoformat(s)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except Exception:
            return datetime.max.replace(tzinfo=timezone.utc)  # push bad rows to the end

    # 1) pick anchor (FIFO head)
    if active_trades:
        anchor = min(
            active_trades,
            key=lambda t: _iso_to_utc(t.get("entry_timestamp") or t.get("transaction_time") or "")
        )

        # 2) compute anchor gate (trails with price OR peak; never moves backward)
        sym = anchor.get("symbol")
        px  = (prices.get(sym) or {}).get("price") if isinstance(prices.get(sym), dict) else prices.get(sym)
        if px is None:
            px = anchor.get("filled_price")  # last resort

        entry = float(anchor.get("filled_price", 0))
        trig  = float(trigger_points)
        off   = float(offset_points)
        side  = anchor.get("action", "BUY").upper()
        peak  = float(anchor.get("trail_peak", entry))
        trough = float(anchor.get("trail_trough", entry))

        if side == "BUY":
            base_gate   = entry + (trig - off)
            trailing_gt = max(px or entry, peak) - off
            gate_price  = max(base_gate, trailing_gt)
            gate_clear  = lambda p: p is not None and p >= gate_price
        else:  # SELL
            base_gate   = entry - (trig - off)
            trailing_gt = min(px or entry, trough) + off
            gate_price  = min(base_gate, trailing_gt)
            gate_clear  = lambda p: p is not None and p <= gate_price

        # 3) enforce (anchor always unlocked; non-anchors parked until gate clears)
        gate_updates = []
        for t in active_trades:
            # attach current gate for visibility
            t["anchor_gate_price"] = gate_price
            t["anchor_order_id"]   = anchor.get("order_id")

            if t is anchor:
                if t.get("gate_state") != "UNLOCKED":
                    t["gate_state"] = "UNLOCKED"
                    gate_updates.append((t.get("order_id"), {"gate_state": "UNLOCKED", "anchor_gate_price": gate_price}))
                continue

            tsym = t.get("symbol")
            tp   = (prices.get(tsym) or {}).get('price') if isinstance(prices.get(tsym), dict) else prices.get(tsym)
            was  = t.get("gate_state", "PARKED")

            if gate_clear(tp):
                if was != "UNLOCKED":
                    t["gate_state"] = "UNLOCKED"
                    t["gate_unlocked_at"] = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
                    t.pop("skip_tp_trailing", None)
                    gate_updates.append((t.get("order_id"), {
                        "gate_state": "UNLOCKED",
                        "gate_unlocked_at": t["gate_unlocked_at"],
                        "anchor_gate_price": gate_price
                    }))
            else:
                t["gate_state"] = "PARKED"
                t["skip_tp_trailing"] = True
                if was != "PARKED" or "gate_state" not in t:
                    gate_updates.append((t.get("order_id"), {"gate_state": "PARKED", "anchor_gate_price": gate_price}))
                    

        # 4) (optional) write gate state to Firebase (best-effort; non-fatal)
        try:
            ref = firebase_db.reference(f"/open_active_trades/{symbol}")
            for oid, payload in gate_updates:
                if oid:
                    ref.child(oid).update(payload)
        except Exception as e:
            print(f"⚠️ Gate state update skipped: {e}")

    # === FILTER: parked trades must not arm TP/Trail ===
    gated_trades = []
    for t in active_trades:
        if t.get("gate_state") == "PARKED" and t.get("skip_tp_trailing"):
            # still allow safety exits elsewhere; just skip trailing TP engine
            continue
        gated_trades.append(t)

    # Trailing TP & exit placement (only for unlocked + anchor)
    print(f"[DEBUG] Processing {len(gated_trades)} trades post AnchorGate")
    try:
        active_trades = process_trailing_tp_and_exits(gated_trades, prices, trigger_points, offset_points)
    except Exception as e:
        print(f"❌ process_trailing_tp_and_exits error: {e}")

   # Trailing TP & exit placement
    print(f"[DEBUG] Processing {len(active_trades)} active trades for trailing TP and exits")
    try:
        active_trades = process_trailing_tp_and_exits(active_trades, prices, trigger_points, offset_points)
    except Exception as e:
        print(f"❌ process_trailing_tp_and_exits error: {e}")

    # [ADD] Track anchors closed in this loop so they cannot be written back
    closed_anchor_ids = set()

    # Only drain exit tickets if we actually have open anchors to match
    if not active_trades:
        print("⏭️ No open anchors; skipping exit ticket drain this loop.")
    else:
        
        # 🔽 EXIT LOGIC: drain exit tickets
        try:
            tickets_ref = firebase_db.reference("/exit_orders_log")
            tickets = tickets_ref.get() or {}
            for tx_id, tx in tickets.items():
                if not isinstance(tx, dict):
                    continue
                if tx.get("_processed"):
                    continue

                # Add symbol if missing (legacy tickets)
                if not tx.get("symbol"):
                    tx["symbol"] = symbol
                    tickets_ref.child(tx_id).update({"symbol": symbol})
                    print(f"[PATCH] Added symbol to stale exit ticket {tx_id}")

                ok = handle_exit_fill_from_tx(firebase_db, tx)

                # [ADD] If handler returned the closed anchor_id, remember it
                if isinstance(ok, str):
                    closed_anchor_ids.add(ok)

                if ok:
                    tickets_ref.child(tx_id).update({"_processed": True})
                    print(f"[INFO] Exit ticket {tx_id} processed and marked _processed")
                else:
                    # stop endless retries on malformed / no-open cases
                    tickets_ref.child(tx_id).update({"_processed": True, "_note": "auto-marked; no opens/malformed"})
                    print(f"[WARN] Exit ticket {tx_id} auto-marked _processed (no opens/malformed)")
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

    # Prevent resurrection: drop just-closed anchors from memory
    if closed_anchor_ids:
        active_trades = [t for t in active_trades if t.get("order_id") not in closed_anchor_ids]

    # Save remaining active trades (strict prune; no closed/exited)
    active_trades = [
        t for t in active_trades
        if t.get('contracts_remaining', 0) > 0
        and not t.get('exited')
        and t.get('status') not in ('closed', 'failed')
    ]
    save_open_trades(symbol, active_trades)
    print(f"[DEBUG] Saved {len(active_trades)} active trades after processing")

    ##========END OF MAIN MONITOR TRADES LOOP FUNCTION========##

# ============================================================================
# 🟩 GREEN PATCH: Invert Grace Period Logic for Stable Zero Position Detection
# ============================================================================

zombie_first_seen = {}

def run_zombie_cleanup_if_ready(trades_list, firebase_db, position_count, grace_period_seconds=90):
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
