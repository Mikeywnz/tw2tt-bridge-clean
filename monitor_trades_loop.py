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
from collections import defaultdict


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

# =====================================================================================
# 🟩 HELPER: trigger & Offset ATR - Lightweight ATR proxy state (EMA of tick ranges) ---
# ======================================================================================
# --- ATR sizing knobs (tamer defaults) ---
_ATR_ALPHA        = 0.20      # smoothing for EMA(|price-ema50|)
ATR_TRIGGER_MULT  = 0.60      # was too high; try 0.6x smoothed range
ATR_OFFSET_MULT   = 0.30      # exit buffer at ~half the trigger

# Floors & caps (keep triggers practical)
MIN_TRIGGER_FLOOR = 2.0
MIN_OFFSET_FLOOR  = 0.8
MAX_TRIGGER_CAP   = 10.0
MAX_OFFSET_CAP    = 4.0
_ema_absdiff = defaultdict(float)

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

def save_open_trades(symbol, trades, grace_seconds: int = 12):

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

        # ---- Guard: skip if an exit is already pending for this trade ----
        try:
            pend_ref = firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}/exit_pending")
            if trade.get("exit_pending") or bool(pend_ref.get()):
                print(f"⏭️ Skip {order_id}: exit_pending is set")
                continue
        except Exception as e:
            print(f"⚠️ exit_pending pre-check failed for {order_id}: {e}")

        direction = 1 if (trade.get('action') or '').upper() == 'BUY' else -1
        price_node = prices.get(symbol)
        current_price = price_node.get('price') if isinstance(price_node, dict) else price_node
        ema50 = price_node.get('ema50') if isinstance(price_node, dict) else None

        if current_price is None:
            print(f"⚠️ No price for {symbol} — skipping {order_id}")
            continue

        entry = trade.get('filled_price')
        if entry is None:
            print(f"❌ Trade {order_id} missing filled_price, skipping.")
            continue

        # ---- Adaptive “ATR-like” range update (per symbol) ----
        try:
            # Use |price - ema50| when available; fall back to 1-tick move |price - entry| as a weak proxy
            global _ema_absdiff
            raw_range = abs((current_price - ema50)) if ema50 is not None else abs(current_price - entry)
            prev = _ema_absdiff.get(symbol, raw_range)
            smoothed = (_ATR_ALPHA * raw_range) + ((1.0 - _ATR_ALPHA) * prev)
            _ema_absdiff[symbol] = smoothed

            adaptive_trigger = max(MIN_TRIGGER_FLOOR,
                                min(MAX_TRIGGER_CAP, ATR_TRIGGER_MULT * smoothed))
            adaptive_offset  = max(MIN_OFFSET_FLOOR,
                                min(MAX_OFFSET_CAP,  ATR_OFFSET_MULT  * smoothed))

            print(f"[ATR] {symbol} smoothed={smoothed:.2f} trig={adaptive_trigger:.2f} off={adaptive_offset:.2f} ema50={ema50}")
            # === LIVE TRAIL SNAPSHOT → Firebase (ATR mode) ===
            try:
                node = firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}")
                trigger_pts = float(adaptive_trigger)
                offset_pts  = float(adaptive_offset)
                trigger_price = (entry + trigger_pts) if direction == 1 else (entry - trigger_pts)
                node.update({
                    "trail_mode": "ATR",
                    "trail_trigger": trigger_pts,
                    "trail_offset":  offset_pts,
                    "trail_trigger_price": trigger_price
                })
                # keep local dict in sync so save_open_trades() preserves fields
                trade["trail_mode"] = "ATR"
                trade["trail_trigger"] = trigger_pts
                trade["trail_offset"]  = offset_pts
                trade["trail_trigger_price"] = trigger_price
            except Exception as e:
                print(f"⚠️ Trail snapshot failed for {order_id}: {e}")

        except Exception as e:
            print(f"⚠️ ATR adapt error for {symbol}: {e}")
            adaptive_trigger = trigger_points
            adaptive_offset  = offset_points

            try:
                node = firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}")
                trigger_price = (entry + adaptive_trigger) if direction == 1 else (entry - adaptive_trigger)
                node.update({
                    "trail_mode": "FALLBACK",
                    "trail_trigger": float(adaptive_trigger),
                    "trail_offset":  float(adaptive_offset),
                    "trail_trigger_price": trigger_price
                })
                # local mirrors
                trade["trail_mode"] = "FALLBACK"
                trade["trail_trigger"] = float(adaptive_trigger)
                trade["trail_offset"]  = float(adaptive_offset)
                trade["trail_trigger_price"] = trigger_price
            except Exception as e2:
                print(f"⚠️ Fallback trail snapshot failed for {order_id}: {e2}")

        # ---- Trigger arming ----
        if not trade.get('trail_hit'):
            trade['trail_peak'] = entry
            trigger_price = entry + adaptive_trigger if direction == 1 else entry - adaptive_trigger
            print(f"[DEBUG] {order_id} trigger @ {trigger_price:.2f} (entry {entry:.2f}, dir {'LONG' if direction==1 else 'SHORT'})")
            if (direction == 1 and current_price >= trigger_price) or (direction == -1 and current_price <= trigger_price):
                trade['trail_hit'] = True
                trade['trail_peak'] = current_price
                print(f"[INFO] TP trigger HIT for {order_id} at {current_price:.2f}")
                try:
                    open_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
                    open_trades_ref.child(order_id).update({
                        "trail_hit": True,
                        "trail_peak": current_price,
                        # first trailing stop price right after arming
                        "trail_stop_price": (current_price - float(adaptive_offset)) if direction == 1 else (current_price + float(adaptive_offset))
                    })
                    # local mirrors
                    trade["trail_hit"] = True
                    trade["trail_peak"] = current_price
                    trade["trail_stop_price"] = (current_price - float(adaptive_offset)) if direction == 1 else (current_price + float(adaptive_offset))
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
                    firebase_db.reference(f"/open_active_trades/{symbol}").child(order_id).update({
                        "trail_peak": new_peak,
                        # keep live trailing stop price visible as peak changes
                        "trail_stop_price": (new_peak - float(adaptive_offset)) if direction == 1 else (new_peak + float(adaptive_offset))
                    })
                    # local mirror
                    trade["trail_stop_price"] = (new_peak - float(adaptive_offset)) if direction == 1 else (new_peak + float(adaptive_offset))
                except Exception as e:
                    print(f"❌ Failed to update trail_peak for {order_id}: {e}")
            else:
                print(f"[DEBUG] Trail peak unchanged for {order_id}: {prev_peak:.2f}")

            buffer_amt = float(adaptive_offset)
            print(f"[DEBUG] Buffer for {order_id}: {buffer_amt:.2f} | price {current_price:.2f} vs peak {trade['trail_peak']:.2f}")

            # === Keep live buffer synced to Firebase ===
            buffer_amt = float(adaptive_offset)
            print(f"[DEBUG] Buffer for {order_id}: {buffer_amt:.2f} | price {current_price:.2f} vs peak {trade['trail_peak']:.2f}")

            # === Keep live buffer synced to Firebase ===
            try:
                firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}").update({
                    "trail_offset": buffer_amt
                })
                # local mirror
                trade["trail_offset"] = buffer_amt
            except Exception:
                pass

            exit_trigger = (
                (direction == 1 and current_price <= trade['trail_peak'] - buffer_amt) or
                (direction == -1 and current_price >= trade['trail_peak'] + buffer_amt)
            )
            if exit_trigger:
                print(f"[INFO] Trailing TP EXIT condition met for {order_id}")

                # ---- Claim: set exit_pending before placing the exit to avoid duplicates ----
                node_ref = firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}")
                try:
                    current = node_ref.get() or {}
                    if current.get("exit_pending"):
                        print(f"⏭️ {order_id} already claimed (exit_pending). Skipping duplicate exit.")
                        continue
                    node_ref.update({"exit_pending": True})
                    trade["exit_pending"] = True  # reflect locally
                except Exception as e:
                    print(f"❌ Failed to claim {order_id} (set exit_pending): {e}")
                    continue

                try:
                    exit_side = 'SELL' if (trade.get('action') or '').upper() == 'BUY' else 'BUY'
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
                            # clear the claim so another attempt can happen
                            try:
                                node_ref.update({"exit_pending": False})
                                trade["exit_pending"] = False
                            except Exception:
                                pass
                        else:
                            print(f"[INFO] Exit ticket enqueued (not processed here): {tx_dict['order_id']}")
                            # leave exit_pending=True until drain closes & archives
                    else:
                        print(f"❌ Exit order failed for {order_id}: {result}")
                        # clear the claim on failure
                        try:
                            node_ref.update({"exit_pending": False})
                            trade["exit_pending"] = False
                        except Exception:
                            pass

                except Exception as e:
                    print(f"❌ Exception placing exit for {order_id}: {e}")
                    # clear the claim on exception
                    try:
                        node_ref.update({"exit_pending": False})
                        trade["exit_pending"] = False
                    except Exception:
                        pass

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
# 🟩 ANCHOR GATE – DROP-IN (Sticky unlock @ +1.0; followers trail; exits blocked until unlock)
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

    # ---------- Sticky unlock config ----------
    cfg = (firebase_db.reference("/trailing_tp_settings").get() or {})
    GATE_UNLOCK_PTS   = float(cfg.get("gate_unlock_points", 1.0))  # points
    HANDOFF_PAUSE_SEC = 2.0  # ~one loop pause after anchor handoff

    # ---------- Track handoff & sticky state on function ----------
    import time
    if not hasattr(monitor_trades, "_last_anchor_id"):
        monitor_trades._last_anchor_id = None
    if not hasattr(monitor_trades, "_handoff_clear_at"):
        monitor_trades._handoff_clear_at = 0.0
    if not hasattr(monitor_trades, "_sticky_unlock"):
        monitor_trades._sticky_unlock = {}  # {symbol: bool}

    anchor_id = anchor.get("order_id")
    symbol    = anchor.get("symbol")

    # Handoff detection → reset sticky, start short pause
    handoff_active = False
    if anchor_id != monitor_trades._last_anchor_id:
        print(f"[INFO] Anchor handoff: {monitor_trades._last_anchor_id} → {anchor_id}")
        monitor_trades._last_anchor_id = anchor_id
        monitor_trades._handoff_clear_at = time.time() + HANDOFF_PAUSE_SEC
        monitor_trades._sticky_unlock[symbol] = False
        handoff_active = True
    else:
        handoff_active = time.time() < monitor_trades._handoff_clear_at

    # ---------- Compute anchor unrealized (points) ----------
    # current price from prices cache
    px = (prices.get(symbol) or {}).get("price") if isinstance(prices.get(symbol), dict) else prices.get(symbol)
    if px is None:
        px = anchor.get("filled_price")  # last resort

    entry = float(anchor.get("filled_price", 0.0))
    side  = (anchor.get("action", "BUY") or "BUY").upper()

    anchor_px = float(px if px is not None else entry)
    anchor_unrealized = (anchor_px - entry) if side == "BUY" else (entry - anchor_px)
    anchor_unrealized = max(0.0, float(anchor_unrealized))

    # ---------- Sticky promotion ----------
    if not monitor_trades._sticky_unlock.get(symbol, False):
        if (not handoff_active) and (anchor_unrealized >= GATE_UNLOCK_PTS):
            monitor_trades._sticky_unlock[symbol] = True
            print(f"[GATE] Sticky unlock ARMED for {symbol} at +{anchor_unrealized:.2f} pts (threshold {GATE_UNLOCK_PTS})")

    followers_unlocked = bool(monitor_trades._sticky_unlock.get(symbol, False))

    # ---------- Apply gate (followers trail, exits blocked until unlocked) ----------
    gate_updates = []
    for t in active_trades:
        t["anchor_order_id"]           = anchor_id
        t["anchor_gate_unlock_pts"]    = GATE_UNLOCK_PTS
        t["anchor_unrealized_pts"]     = round(anchor_unrealized, 2)
        t["sticky_followers_unlocked"] = followers_unlocked

        if t is anchor:
            if t.get("gate_state") != "UNLOCKED":
                t["gate_state"] = "UNLOCKED"
                gate_updates.append((t.get("order_id"), {"gate_state": "UNLOCKED"}))
            t["block_exit"] = False
            continue

        if followers_unlocked:
            if t.get("gate_state") != "UNLOCKED" or t.get("block_exit"):
                t["gate_state"] = "UNLOCKED"
                t["block_exit"] = False
                gate_updates.append((t.get("order_id"), {"gate_state": "UNLOCKED", "block_exit": False}))
        else:
            t["gate_state"] = "PARKED"
            t["block_exit"] = True     # exits blocked; trailing still allowed
            if t.get("_sent_park") is not True:
                gate_updates.append((t.get("order_id"), {"gate_state": "PARKED", "block_exit": True}))
                t["_sent_park"] = True

    # 4) best-effort write gate state to Firebase
    try:
        ref = firebase_db.reference(f"/open_active_trades/{symbol}")
        for oid, payload in gate_updates:
            if oid:
                ref.child(oid).update(payload)
    except Exception as e:
        print(f"⚠️ Gate state update skipped: {e}")

# === Followers still trail while parked ===
gated_trades = active_trades[:]  # do NOT filter parked trades out
print(f"[DEBUG] Processing {len(gated_trades)} trades post AnchorGate")

# ✅ Single call (remove any duplicate second call)
try:
    active_trades = process_trailing_tp_and_exits(gated_trades, prices, trigger_points, offset_points)
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

            if not tx.get("symbol"):
                tx["symbol"] = symbol
                tickets_ref.child(tx_id).update({"symbol": symbol})
                print(f"[PATCH] Added symbol to stale exit ticket {tx_id}")

            ok = handle_exit_fill_from_tx(firebase_db, tx)

            if isinstance(ok, str):
                closed_anchor_ids.add(ok)

            if ok:
                tickets_ref.child(tx_id).update({"_processed": True})
                print(f"[INFO] Exit ticket {tx_id} processed and marked _processed")
            else:
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

if closed_anchor_ids:
    active_trades = [t for t in active_trades if t.get("order_id") not in closed_anchor_ids]

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

def run_zombie_cleanup_if_ready(trades_list, firebase_db, position_count, grace_period_seconds=140):
    now = time.time()
    open_trades_ref = firebase_db.reference("/open_active_trades")
    zombie_trades_ref = firebase_db.reference("/zombie_trades_log")

    for trade in trades_list:
        order_id = trade.get("order_id")
        symbol = trade.get("symbol", "UNKNOWN")

        # If we hold any position, clear timers and skip
        if position_count != 0:
            if order_id in zombie_first_seen:
                print(f"✅ Trade {order_id} on {symbol} no longer zero global position; clearing timer")
                zombie_first_seen.pop(order_id, None)
            continue

        # Zero global position → start or advance timer
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

if __name__ == '__main__':
    while True:
        try:
            monitor_trades()
        except Exception as e:
            print(f"❌ ERROR in monitor_trades(): {e}")
        time.sleep(10)

# =========================  END OF SCRIPT ================================
