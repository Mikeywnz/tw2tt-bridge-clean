#=========================  PUSH_ORDERS_TO_FIREBASE - PART 1  ================================
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType, OrderStatus  # ✅ correct on Render!
import random
import string
from datetime import datetime, timedelta, timezone as dt_timezone
from pytz import timezone
import requests
import json
import firebase_active_contract
import firebase_admin
from firebase_admin import credentials, initialize_app, db
import os
import time
from typing import Optional
import datetime as dt

grace_cache = {}
_logged_order_ids = set()

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


#================================
# 🟩 TIGER API SET UP ============
#================================
config = TigerOpenClientConfig()
client = TradeClient(config)

#################### ALL HELPERS FOR THIS SCRIPT ####################

# ==================================================
# 🟩 Helper: Time helpers ===
# ==================================================

def _safe_iso(val) -> str:
    """
    Return a UTC ISO8601 string like 'YYYY-MM-DDTHH:MM:SS.sssZ'
    Accepts: ISO string (naive or tz), epoch (ms/sec), or None.
    """
    try:
        # epoch?
        if isinstance(val, (int, float)):
            ts = float(val)
            if ts > 1e12:  # ms -> sec
                ts /= 1000.0
            d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
            return d.isoformat().replace("+00:00", "Z")

        s = (str(val) or "").strip()
        if not s:
            raise ValueError("empty")

        if s.endswith("Z"):
            return s  # already UTC ISO

        d = dt.datetime.fromisoformat(s)
        d = d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        d = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        return d.isoformat().replace("+00:00", "Z")

# ==================================================
# 🟩 Helper: Map Source, Get exit reason helpers ===
# ==================================================

def map_source(raw_source):
    if raw_source is None:
        return "unknown"
    lower = raw_source.lower()
    if "openapi" in lower:
        return "OpGo"
    elif "desktop" in lower:
        return "Tiger Desktop"
    elif "mobile" in lower:
        return "tiger-mobile"
    elif "liquidation" in lower:
        return "Tiger Liquidation"
    return "unknown"

def get_exit_reason(status, reason, filled, is_open=False):
    if status == "CANCELLED" and filled == 0:
        return "CANCELLED"
    elif status == "EXPIRED" and filled == 0 and reason and ("资金" in reason or "margin" in reason.lower()):
        return "LACK_OF_MARGIN"
    elif "liquidation" in reason.lower():
        return "liquidation"
    elif status == "FILLED":
        return "FILLED"
    return status

# ==================================================
# 🟩 Helper: read IDs from flat or {symbol}/... logs
# ==================================================
def _log_ids_for(dbh, path, symbol):
    try:
        node = dbh.reference(path).get() or {}
    except Exception:
        return set()
    ids = set()
    if isinstance(node, dict):
        # symbol-scoped
        sym_child = node.get(symbol)
        if isinstance(sym_child, dict):
            ids |= set(map(str, sym_child.keys()))
        # flat layout (ids at top-level)
        for k in node.keys():
            if str(k).isdigit():
                ids.add(str(k))
    return ids

# ====================================================
# 🟩 Helper: Load_trailing_tp_settings() From Firebase
# ====================================================
def load_trailing_tp_settings():
    try:
        ref = db.reference('/trailing_tp_settings')
        cfg = ref.get() or {}

        if cfg.get("enabled", False):
            return float(cfg.get("trigger_points", 14.0)), float(cfg.get("offset_points", 5.0))
    except Exception as e:
        print(f"⚠️ Failed to fetch trailing TP settings: {e}")
    return 14.0, 5.0


#===============================================
# 🟩 Helper: Check if Trade ID is a Known Zombie
#===============================================

def is_zombie_trade(order_id, firebase_db, symbol=None):
    if not order_id:
        return False
    # symbol-scoped
    if symbol and firebase_db.reference(f"/zombie_trades_log/{symbol}/{order_id}").get():
        return True
    # flat
    return bool(firebase_db.reference(f"/zombie_trades_log/{order_id}").get())

#======================================================
# 🟩 Helper: Check if Trade ID is a Known Archived Trade
#======================================================

def is_archived_trade(order_id, firebase_db, symbol=None):
    if not order_id:
        return False
    if symbol and firebase_db.reference(f"/archived_trades_log/{symbol}/{order_id}").get():
        return True
    return bool(firebase_db.reference(f"/archived_trades_log/{order_id}").get())

# ====================================================
#🟩 Helper: to Check if Trade ID is a Known Ghost Trade
# ====================================================
def is_ghostflag_trade(order_id, firebase_db, symbol=None):
    if not order_id:
        return False
    if symbol and firebase_db.reference(f"/ghost_trades_log/{symbol}/{order_id}").get():
        return True
    return bool(firebase_db.reference(f"/ghost_trades_log/{order_id}").get())

#################### END OF ALL HELPERS FOR THIS SCRIPT ####################
    
# =======================================================
# ======MAIN FUNCTION ==PUSH ORDERS TO FIREBASE==========
# =======================================================
def push_orders_main():

    #=======Definitions ===========
    symbol = firebase_active_contract.get_active_contract()
     #=====Time function ===
    now = datetime.utcnow()
   
    #====Initialize counters here, BEFORE the order loop: =====
    open_trades_count = 0
    cancelled_count = 0
    lack_margin_count = 0
    unknown_count = 0

    # Reason map dictionary to more user friendly names
    REASON_MAP = {
        "trailing_tp_exit": "Trailing Take Profit",
        "manual_close": "Manual Close",
        "ema_flattening_exit": "EMA Flattening",
        "liquidation": "Liquidation",
        "LACK_OF_MARGIN": "Lack of Margin",
        "CANCELLED": "Cancelled",
        "EXPIRED": "Lack of Margin",
    }

    # ================== Use Active Contract Symbol For Efficiency ====================
    # Before fetching orders from TigerTrade API, fetch the active contract from Firebase
    active_symbol = firebase_active_contract.get_active_contract()
    if not active_symbol:
        print("❌ No active contract symbol found in Firebase; aborting orders fetch")
        return 

    # Default small fetch
    limit = 20

    # If we detect recent bursts, widen temporarily
    if getattr(push_orders_main, "_recent_burst", False):
        limit = 50
        push_orders_main._recent_burst = False # reset after one wide fetch

    orders = client.get_orders(
    account="21807597867063647",
    seg_type=SegmentType.FUT,
    limit=limit
    )
    print(f"\n📦 Total FUT orders returned (limit={limit}): {len(orders)}")

   #=========================================================================================
    # ====================== START THE FUNCTION: Push Orders Processing ======================
    #=========================================================================================

    # 🟩 Refresh archived trades cache inside main loop
    archived_order_ids = _log_ids_for(firebase_db, "/archived_trades_log", active_symbol)

    tiger_ids = set()

    for order in orders:
        try:
            # Always use the TigerTrade long ID from get_orders()
            order_id = str(getattr(order, 'id', '')).strip()
            active_symbol = getattr(order, "symbol", "")

            # Single validation – ensures it's a numeric string
            if not (isinstance(order_id, str) and order_id.isdigit()):
                print(f"❌ Skipping order due to invalid order_id: '{order_id}'. Order raw data: {order}")
                continue

          # 🔐 EARLY EXIT-TICKET FENCE — block exit fills from being processed as opens
            if firebase_db.reference(f"/exit_orders_log/{active_symbol}/{order_id}").get() \
            or firebase_db.reference(f"/exit_orders_log/{order_id}").get():
                print(f"⏭️ Skipping EXIT ticket {order_id} (early fence)")
                continue

            # ✅ Route Tiger liquidations as exit tickets (do NOT touch open_active_trades here)
            if getattr(order, "liquidation", False) is True:
                liq_oid = str(getattr(order, "id", "") or getattr(order, "order_id", "")).strip()  # Tiger uses 0 for order_id; use 'id'
                liq_px  = (getattr(order, "avg_fill_price", None)
                        or getattr(order, "filled_price", None)
                        or getattr(order, "latest_price", None)
                        or 0.0)
                liq_ts  = (getattr(order, "update_time", None)
                        or getattr(order, "trade_time", None)
                        or getattr(order, "order_time", None))
                liq_iso = _safe_iso(liq_ts)
                liq_side = str(getattr(order, "action", "") or "").upper()
                liq_sym  = getattr(order, "symbol", "") or active_symbol

                firebase_db.reference(f"/exit_orders_log/{liq_sym}/{liq_oid}").set({
                    "order_id": liq_oid,
                    "symbol": liq_sym,
                    "action": liq_side,
                    "filled_price": float(liq_px),
                    "filled_qty": int(getattr(order, "quantity", 1) or 1),
                    "fill_time": liq_iso,
                    "status": "LIQUIDATION",
                    "trade_type": "LIQUIDATION"
                })
                print(f"[LIQ] Queued liquidation as exit ticket {liq_oid} for {liq_sym} at {liq_px}")
                continue

                        # ✅ Manual manual exits (desktop or mobile) → enqueue exit ticket (no open_active_trades writes)
            #    - Sources: desktop-mac / desktop / ios / iphone / ipad / android / tiger-mobile / mobile
            try:
                src_raw = str(getattr(order, "source", "") or "").strip().lower()
                manual_sources = {"desktop-mac", "desktop", "ios", "iphone", "ipad", "android", "tiger-mobile", "mobile"}
                if src_raw in manual_sources:
                    # pick the freshest Tiger time (ms since epoch)
                    ts_ms = (getattr(order, "update_time", None)
                            or getattr(order, "trade_time", None)
                            or getattr(order, "order_time", None))
                    if ts_ms:
                        now_s = time.time()
                        t_s   = float(ts_ms) / 1000.0
                        age   = now_s - t_s  # seconds
                        if age <= 180.0:  # <= 3 minutes
                            man_oid = str(getattr(order, "id", "") or getattr(order, "order_id", "")).strip()
                            if man_oid and man_oid.isdigit():
                                man_px   = (getattr(order, "avg_fill_price", None)
                                            or getattr(order, "filled_price", None)
                                            or getattr(order, "latest_price", None)
                                            or 0.0)
                                man_iso  = _safe_iso(ts_ms)
                                man_side = str(getattr(order, "action", "") or "").upper()
                                man_sym  = getattr(order, "symbol", "") or active_symbol

                                firebase_db.reference(f"/exit_orders_log/{man_sym}/{man_oid}").update({
                                    "order_id": man_oid,
                                    "symbol": man_sym,
                                    "action": man_side,
                                    "filled_price": float(man_px),
                                    "filled_qty": 1,                  # close 1 FIFO leg
                                    "fill_time": man_iso,
                                    "status": "FILLED",
                                    "trade_type": "MANUAL_EXIT",
                                    "source": src_raw,                 # preserve exact source (e.g., "ios")
                                })
                                print(f"[MANUAL] Queued manual exit ticket {man_oid} ({src_raw}) for {man_sym} at {man_px} (age {age:.1f}s)")
                                # Do NOT touch /open_active_trades here; FIFO drain will close it.
                                continue
                            else:
                                print(f"[MANUAL] recent manual order has non-numeric id; skipping: id={man_oid}, source={src_raw}")
                        else:
                            print(f"[MANUAL] ignored stale manual order (age {age:.1f}s, source={src_raw})")
            except Exception as e:
                print(f"⚠️ Manual exit block failed softly: {e}")

            # ===================== Check if order ID is already processed and filter out ====================

            if not order_id:
                print("⚠️ Skipping order with empty or missing order_id")
                continue
            print(f"🔍 Processing order_id: {order_id}")

            if is_zombie_trade(order_id, db, active_symbol):
                print(f"⏭️ ⛔ Skipping zombie trade {order_id} during API push")
                continue
            else:
                print(f"✅ Order ID {order_id} not a zombie, proceeding")

            if is_archived_trade(order_id, db, active_symbol):
                print(f"⏭️ ⛔ Skipping archived trade {order_id} during API push")
                continue

            if is_ghostflag_trade(order_id, db, active_symbol):
                print(f"⏭️ ⛔ Skipping ghost trade {order_id} during API push (detected by helper)")
                continue
            else:
                print(f"✅ Order ID {order_id} not a ghost, proceeding")


            # ===================================End of First filtering=======================================

            #=========================== Extract order information for further processing ====================
            tiger_ids.add(order_id)

            raw_status = getattr(order, "status", "")
            status = "FILLED" if raw_status == "SUCCESS" else str(raw_status).split('.')[-1].upper()
            raw_reason = getattr(order, "reason", "")
            filled = getattr(order, "filled", 0)
            is_open = getattr(order, "is_open", False)
            exit_reason_raw = get_exit_reason(status, raw_reason, filled, is_open)

            #==========================================================================================
            # 🚫 Hard rule: never accept Tiger orders whose status is FILLED.
            # (Prevents exit fills & historical fills from reappearing as new opens.)
            #SUPER IMPORTANT CODE THAT STOP OPEN ORDERS GETTING FILLED WITH JUNK ++ TEMPEORY REPLACEMENT 
            if status == "FILLED":
                print(f"⏭️ Skipping FILLED order {order_id} for {active_symbol}")
                continue
            #==========================================================================================

            # ===== NO-MAN'S-LAND GUARD: treat truly closed orders as closed, not opens =====
            status_up = str(getattr(order, 'status', '')).split('.')[-1].upper()
            is_open   = bool(getattr(order, 'is_open', False))

            # CLOSED if: explicit CLOSED/EXPIRED/CANCELLED, OR FILLED but not open.
            is_truly_closed = (
                status_up in {'CLOSED', 'EXPIRED', 'CANCELLED'} or
                (status_up == 'FILLED' and not is_open)
            )

            if is_truly_closed:
                # Build a minimal record for logging/archiving without touching open_active_trades
                closed_payload = {
                    "order_id":        order_id,
                    "symbol":          active_symbol,
                    "status":          status_up,
                    "is_open":         is_open,
                    "filled":          int(getattr(order, "filled", 0) or 0),
                    "action":          str(getattr(order, 'action', '')).upper(),
                    "reason":          str(getattr(order, "reason", "") or status_up),
                    "source":          map_source(getattr(order, 'source', None)),
                    "order_time":      getattr(order, "order_time", None),
                    "update_time":     getattr(order, "update_time", None),
                    "is_ghost":        False,
                    "trade_state":     "closed",
                }

                try:
                    firebase_db.reference(f"/archived_trades_log/{active_symbol}/{order_id}").set(closed_payload)
                    print(f"🗄️ Archived closed trade {order_id} to /archived_trades_log")
                except Exception as e:
                    print(f"⚠️ Archive failed for closed trade {order_id}: {e}")

                print(f"⚠️ Skipping closed trade {order_id} for open_active_trades push")
                continue

            # 🧱 GHOST GATE — EXPIRED / CANCELLED / LACK_OF_MARGIN
            ghost_statuses = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
            is_ghost = (status in ghost_statuses) or (not is_open and filled == 0 and status != "FILLED")

            if is_ghost:
                reason_text = (str(raw_reason) or status).strip()
                ghost_record = {
                    "order_id": order_id,
                    "symbol": active_symbol,  # already resolved above
                    "status": status,
                    "reason": reason_text,
                    "filled": int(filled or 0),
                    "is_open": bool(is_open),
                    "ghost": True,
                    "source": map_source(getattr(order, 'source', None)),
                    "order_time": getattr(order, "order_time", None),
                    "update_time": getattr(order, "update_time", None),
                }
                try:
                    # 1) Archive (audit)
                    firebase_db.reference(f"/archived_trades_log/{active_symbol}/{order_id}").set(ghost_record)
                    # 2) Index in ghost log
                    firebase_db.reference(f"/ghost_trades_log/{active_symbol}/{order_id}").set(ghost_record)
                    # 3) Remove any live copy from open_active_trades
                    open_ref = firebase_db.reference(f"/open_active_trades/{active_symbol}")
                    if open_ref.child(order_id).get() is not None:
                        open_ref.child(order_id).delete()
                        print(f"🗑️ Removed ghost {order_id} from /open_active_trades/{active_symbol}")
                    print(f"👻 Archived ghost trade {order_id} ({status}: {reason_text})")
                except Exception as e:
                    print(f"❌ Ghost archive/delete failed for {order_id}: {e}")
                continue

            # ======================= Normalize TigerTrade timestamp (raw ms → ISO UTC) ======================
            raw_ts = getattr(order, 'order_time', 0)
            try:
                ts_dt = datetime.utcfromtimestamp(raw_ts / 1000.0)
                exit_time_str = ts_dt.strftime("%Y-%m-%d %H:%M:%S")  # For Google Sheets
            except Exception as e:
                print(f"⚠️ Failed to parse Tiger order_time: {raw_ts} → {e}")
                exit_time_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                exit_reason_raw = "UNKNOWN"

            symbol = firebase_active_contract.get_active_contract()
            if not symbol:
                print(f"❌ No active contract symbol found in Firebase; skipping order ID {order_id}")
                continue  # Skip processing this order

            existing_trade = firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}").get() or {}
            trigger_points, offset_points = load_trailing_tp_settings()

            # Keep original entry timestamp if it exists
            entry_timestamp = existing_trade.get("entry_timestamp")
            if not entry_timestamp:
                entry_timestamp = getattr(order, "transaction_time", None) or datetime.utcnow().isoformat() + "Z"

            filled_price_final = existing_trade.get("filled_price", 0.0)
            just_executed_final = existing_trade.get("just_executed", False)
            trail_hit = existing_trade.get("trail_hit", False)
            trail_peak = existing_trade.get("trail_peak", existing_trade.get("filled_price"))
            trade_type_final = existing_trade.get("trade_type", "")
            if getattr(order, "trade_type", None):
                trade_type_final = getattr(order, "trade_type")

        except Exception as e:
            print(f"❌ push_orders_main pre-update error for order {locals().get('order_id', '<unknown>')}: {e}")
            continue

        # ========================= BUILD PAYLOAD READY TO PUSH TO FIREBASE ================================
        print(f"[DEBUG] existing_trade data: {existing_trade}")
        print(f"[DEBUG] filled_price_final: {filled_price_final}")

        payload = {
            "order_id": order_id,
            "symbol": symbol,
            "filled_price": filled_price_final,  # preserve original from app.py
            "action": str(getattr(order, 'action', '')).upper(),
            "trade_type": trade_type_final,
            "status": status,
            "contracts_remaining": getattr(order, "contracts_remaining", 1),
            "trail_trigger": existing_trade.get("trail_trigger", trigger_points),
            "trail_offset": existing_trade.get("trail_offset", offset_points),
            "trail_hit": trail_hit,                                   # preserve
            "trail_peak": trail_peak,                                 # preserve
            "filled": bool(filled),
            "entry_timestamp": entry_timestamp,                       # sticky
            "just_executed": just_executed_final,                     # sticky
            "exit_timestamp": existing_trade.get("exit_timestamp") or None,  # preserve only
            "trade_state": "open" if status == "FILLED" and is_open else "closed",
            "quantity": getattr(order, 'quantity', 0),
            "realized_pnl": 0.0,
            "net_pnl": 0.0,
            "tiger_commissions": 0.0,
            "exit_reason": existing_trade.get("exit_reason", exit_reason_raw),  # raw; FIFO will prettify
            "liquidation": getattr(order, 'liquidation', False),
            "source": map_source(getattr(order, 'source', None)),
            "is_open": getattr(order, 'is_open', False),
            "is_ghost": False,
        }

            # 🛡️ Do not resurrect closed/exited trades
        if existing_trade.get("exited") or existing_trade.get("trade_state") == "closed":
            print(f"⏭️ Not resurrecting closed trade {order_id}; skipping write.")
            continue
        # 🛑 Never write ghosts into open_active_trades
        if payload.get("is_ghost"):
            print(f"👻 Skipping write of ghost {order_id} to /open_active_trades/{symbol}")
            continue

        # 🔒 LATE EXIT‑TICKET FENCE — last check before we touch /open_active_trades
        if firebase_db.reference(f"/exit_orders_log/{active_symbol}/{order_id}").get() \
        or firebase_db.reference(f"/exit_orders_log/{order_id}").get():
            print(f"⏭️ Skipping EXIT ticket {order_id} (late fence)")
            continue

        ref = firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}")
        try:
            existing_trade = ref.get() or {}

            # Merge-only: never create new open trades here
            if not existing_trade:
                print(f"⏭️ Merge-only: skipping new order {order_id} (no existing open trade in Firebase)")
                continue

            # Safe merge (hard FILLED-skip ran earlier; closed-trade guard ran earlier)
            merged_trade = {**existing_trade, **payload}
            ref.update(merged_trade)
            print(f"✅ Merged into existing open trade {order_id}")

        except Exception as e:
            print(f"❌ Failed to upsert /open_active_trades/{symbol}/{order_id}: {e}")

        
            # ===========🟩 Skip if trade already archived ghost or Zombie and cached in the new run coming up=====================
            if order_id in archived_order_ids:
                print(f"⏭️ ⛔ Archived trade {order_id} already processed this run; skipping duplicate archive")
                continue

            if is_ghostflag_trade(order_id, db):
                print(f"⏭️ ⛔ Skipping ghost trade {order_id} during API push (detected by helper)")
                continue

            if is_zombie_trade(order_id, db):
                print(f"⏭️ ⛔ Skipping zombie trade {order_id} during API push (detected by helper)")
                continue

            print(f"Order ID {order_id} not archived, ghost, or zombie; proceeding")

            if payload.get("is_open", False):
                open_trades_count += 1
            elif exit_reason_raw == "CANCELLED":
                cancelled_count += 1
            elif exit_reason_raw == "LACK_OF_MARGIN":
                lack_margin_count += 1
            else:
                unknown_count += 1

            archived_order_ids.add(order_id)

            # Use order_id safely from here on
            ref = db.reference(f'/open_active_trades/{symbol}/{order_id}')

            # Simple guard: skip closed but filled trades
            if not payload.get("is_open", False) and payload.get("status", "").upper() == "FILLED":
                print(f"⚠️ Skipping closed filled trade {payload.get('order_id')} before open check")
                continue

        except Exception as e:
            print(f"❌ Error processing order {order}: {e}")
            continue

    # --- Burst detector (place AFTER the loop, BEFORE the heartbeat) ---
    try:
        last_seen = getattr(push_orders_main, "_last_seen_id", 0)
        new_ids = [int(getattr(o, "id", 0) or getattr(o, "order_id", 0)) for o in orders]
        new_max = max(new_ids) if new_ids else last_seen
        fresh = [oid for oid in new_ids if int(oid) > int(last_seen)]
        if len(fresh) >= 15:
            push_orders_main._recent_burst = True
            print(f"[BURST] {len(fresh)} new orders since {last_seen} → widen next fetch to 50")
        push_orders_main._last_seen_id = new_max
    except Exception as e:
        print(f"[BURST] detector skipped: {e}")

    # ======= Ensure /open_active_trades/ path stays alive, even if no trades written =====
    try:
        open_active_trades_root = db.reference("/open_active_trades")
        snapshot = open_active_trades_root.get() or {}
        if not snapshot:
            print("🫀 Writing /open_active_trades/_heartbeat to keep path alive")
            open_active_trades_root.child("_heartbeat").set("alive")
    except Exception as e:
        print(f"❌ Failed to write /open_active_trades/_heartbeat: {e}")

#=============================================================================================================================================
#=============================================END OF MAIN PUSH_ORDERS_MAIN_FUNCTION===========================================================
#=============================================================================================================================================

if __name__ == "__main__":
    import time
    while True:
        try:
            push_orders_main()  # <-- your existing main function
        except Exception as e:
            print(f"❌ Error running push_orders_main(): {e}")
        time.sleep(20)  # wait 20 seconds before next loop
#===============================================================(END OF SCRIPT) ======================================================================