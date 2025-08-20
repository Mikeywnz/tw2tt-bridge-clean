#=========================  PUSH_ORDERS_TO_FIREBASE - PART 1  ================================
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType, OrderStatus  # ✅ correct on Render!
import random
import string
from datetime import datetime, timedelta
from pytz import timezone
import requests
import json
import firebase_active_contract
import firebase_admin
from firebase_admin import credentials, initialize_app, db
import os
import time
from typing import Optional


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

# =====================================================
# 🟩 Helper: Safe int cast for sorting entry_timestamps
# =====================================================
def safe_int(value):
    try:
        return int(value)
    except:
        return 0

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

archived_trades_ref = db.reference("/archived_trades_log")
archived_order_ids = set(archived_trades_ref.get() or {})
    
#===============================================
# 🟩 Helper: Check if Trade ID is a Known Zombie
#===============================================

def is_zombie_trade(order_id, firebase_db):
    zombie_ref = firebase_db.reference("/zombie_trades_log")
    zombies = zombie_ref.get() or {}
    return order_id in zombies

#======================================================
# 🟩 Helper: Check if Trade ID is a Known Archived Trade
#======================================================

def is_archived_trade(order_id, firebase_db):
    archived_ref = firebase_db.reference("/archived_trades_log")
    archived_trades = archived_ref.get() or {}
    return order_id in archived_trades

# ====================================================
#🟩 Helper: to Check if Trade ID is a Known Ghost Trade
# ====================================================
def is_ghostflag_trade(order_id, firebase_db):
    ghost_ref = firebase_db.reference("/ghost_trades_log")
    ghosts = ghost_ref.get() or {}
    return order_id in ghosts


# ====================================================
# 🟩 Helper: Liquidation Handler (hardened)
# ====================================================

from typing import Optional, Union
from datetime import datetime, timezone

def _safe_iso(ts_ms: Optional[int]) -> str:
    """Best-effort: Tiger ms -> ISO Z; fall back to now."""
    try:
        if ts_ms is not None:
            return datetime.utcfromtimestamp(int(ts_ms) / 1000).isoformat() + "Z"
    except Exception:
        pass
    return datetime.utcnow().isoformat() + "Z"

def _to_epoch_utc(iso_like: Optional[Union[str, int, float]]) -> float:
    """
    Tolerant timestamp → epoch seconds (UTC).
    - Accepts ms/seconds numbers or ISO-ish strings ('YYYY-MM-DD HH:MM:SS' or '...T...Z', optional '+00:00').
    - Returns +inf when missing/bad so those sort *last* (i.e., won't be picked as FIFO).
    """
    if iso_like is None:
        return float("inf")

    # Numeric: ms/seconds
    if isinstance(iso_like, (int, float)):
        try:
            x = float(iso_like)
            # Heuristic: > 1e12 ≈ ms; > 1e10 also likely ms
            if x > 1e10:
                x = x / 1000.0
            return datetime.utcfromtimestamp(x).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return float("inf")

    # String: normalize some common variants
    s = str(iso_like).strip()
    if not s:
        return float("inf")
    s = s.replace(" ", "T")
    if s.endswith("+00:00"):
        s = s[:-6] + "Z"
    try:
        # allow trailing Z (UTC) or naive (treat as UTC)
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc).timestamp()
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return float("inf")

def handle_liquidation_fifo(firebase_db, symbol, order_obj) -> Optional[str]:
    """
    Archives + deletes the FIFO open trade for `symbol` when Tiger flags liquidation.
    Returns the closed anchor_id (str) if something was archived/deleted, else None.
    """
    # --- Pull liquidation fill details (best-effort) ---
    exit_oid  = str(getattr(order_obj, "id", "") or getattr(order_obj, "order_id", "")).strip()
    exit_px   = (getattr(order_obj, "avg_fill_price", None)
                 or getattr(order_obj, "filled_price", None)
                 or getattr(order_obj, "latest_price", None)
                 or 0.0)
    exit_time = (getattr(order_obj, "update_time", None)
                 or getattr(order_obj, "trade_time", None)
                 or getattr(order_obj, "order_time", None))
    exit_iso  = _safe_iso(exit_time)
    exit_side = str(getattr(order_obj, "action", "") or "").upper()  # BUY/SELL
    status_up = str(getattr(order_obj, "status", "")).split(".")[-1].upper() or "LIQUIDATION"

    # --- Load opens and keep only real trade dicts (ignore _heartbeat, strings, etc.) ---
    open_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
    raw_opens = open_ref.get() or {}

    if not isinstance(raw_opens, dict):
        print(f"[LIQ] Unexpected /open_active_trades/{symbol} type: {type(raw_opens)}; nothing to close.")
        return None

    opens = {}
    for k, v in raw_opens.items():
        if not isinstance(v, dict):
            continue
        # Must look like a trade
        if not (v.get("order_id") or (isinstance(k, str) and k.isdigit())):
            continue
        opens[k] = v

    if not opens:
        print(f"[LIQ] No valid open trades under /open_active_trades/{symbol}; nothing to close.")
        return None

    # --- FIFO select: earliest usable timestamp wins ---
    def _ts_key(rec: dict) -> float:
        # Try multiple fields; bad/missing → +inf (sorted last)
        return min(
            _to_epoch_utc(rec.get("entry_timestamp")),
            _to_epoch_utc(rec.get("transaction_time")),
            _to_epoch_utc(rec.get("order_time")),
        )

    try:
        anchor_oid, anchor = min(opens.items(), key=lambda kv: _ts_key(kv[1]))
    except Exception as e:
        print(f"[LIQ] FIFO selection failed for {symbol}: {e}")
        return None

    if not isinstance(anchor, dict):
        print(f"[LIQ] Selected FIFO anchor malformed for {symbol}: type={type(anchor)}")
        return None

    # --- P&L (best-effort) based on entry side ---
    try:
        entry_px = float(anchor.get("filled_price") or 0.0)
    except Exception:
        entry_px = 0.0
    try:
        qty = int(anchor.get("quantity") or 1)
    except Exception:
        qty = 1
    side_in  = str(anchor.get("action", "")).upper()  # BUY/SELL
    pnl_raw  = (float(exit_px) - entry_px) * qty if side_in == "BUY" else (entry_px - float(exit_px)) * qty

    update = {
        "exited": True,
        "exit_reason": "LIQUIDATION",
        "exit_timestamp": exit_iso,
        "exit_order_id": exit_oid,
        "exit_action": exit_side,
        "filled_exit_price": float(exit_px),
        "status": status_up,
        "realized_pnl": float(pnl_raw),
        "is_open": False,
        "trade_state": "closed",
        "liquidation": True,
        "contracts_remaining": 0,
        "just_executed": False,
    }

       # --- Archive + delete (immediate) ---
    try:
        archive_ref = firebase_db.reference(f"/archived_trades_log/{anchor_oid}")
        archive_ref.set({**anchor, **update})
        open_ref.child(anchor_oid).delete()
        print(f"[LIQ] Archived+deleted FIFO anchor {anchor_oid} ({symbol}) at {exit_px} — reason LIQUIDATION")

        # 📝 Also record a liquidation ticket for Sheets drain
        try:
            firebase_db.reference(f"/exit_orders_log/{exit_oid}").set({
                "order_id": exit_oid,
                "symbol": symbol,
                "action": exit_side,
                "filled_price": float(exit_px),
                "filled_qty": qty,
                "fill_time": exit_iso,
                "status": "LIQUIDATION",
                "trade_type": "LIQUIDATION",
                "anchor_id": anchor_oid,
            })
            print(f"[LIQ] Logged liquidation ticket {exit_oid} for {symbol}")
        except Exception as e:
            print(f"[LIQ] ⚠️ Failed to log liquidation ticket {exit_oid}: {e}")

        return anchor_oid
    except Exception as e:
        print(f"[LIQ] ❌ Archive/delete failed for {anchor_oid}: {e}")
        return None

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

    # Then pass active_symbol as a filter To Tiger Trade through client.get_orders()
    orders = client.get_orders(
        account="21807597867063647",
        seg_type=SegmentType.FUT,
        symbol=active_symbol,  # if you added this from patch
        limit=20
    )
    print(f"\n📦 Total orders returned for active contract {active_symbol}: {len(orders)}")

   #=========================================================================================
    # ====================== START THE FUNCTION: Push Orders Processing ======================
    #=========================================================================================

    # 🟩 Refresh archived trades cache inside main loop
    archived_order_ids = set(archived_trades_ref.get() or {})

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
            exit_ref_early = firebase_db.reference(f"/exit_orders_log/{order_id}")
            if exit_ref_early.get():
                print(f"⏭️ Skipping EXIT ticket {order_id} (early fence)")
                continue

            # Send liqudations to Liquidation handler 
            if getattr(order, "liquidation", False) is True:
                active_symbol = getattr(order, "symbol", "") or active_symbol
                print(f"🔥 Detected TigerTrade liquidation for {order_id} – invoking FIFO archive/delete.")
                closed_anchor = handle_liquidation_fifo(firebase_db, active_symbol, order)
                continue

            # ===================== Check if order ID is already processed and filter out ====================

            if not order_id:
                print("⚠️ Skipping order with empty or missing order_id")
                continue
            print(f"🔍 Processing order_id: {order_id}")

            if is_zombie_trade(order_id, db):
                print(f"⏭️ ⛔ Skipping zombie trade {order_id} during API push")
                continue
            else:
                print(f"✅ Order ID {order_id} not a zombie, proceeding")

            if is_archived_trade(order_id, db):
                print(f"⏭️ ⛔ Skipping archived trade {order_id} during API push")
                continue

            if is_ghostflag_trade(order_id, db):
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

            # 🚫 Hard rule: never accept Tiger orders whose status is FILLED.
            # (Prevents exit fills & historical fills from reappearing as new opens.)
            if status == "FILLED":
                print(f"⏭️ Skipping FILLED order {order_id} for {active_symbol}")
                continue

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

                # Optional: your Sheets hook if you want it for closed orders
                try:
                    log_payload_as_closed_trade(closed_payload)
                    print(f"✅ Logged closed trade {order_id} to Sheets (No-Man's-Land)")
                except Exception as e:
                    print(f"⚠️ Sheets log failed for closed trade {order_id}: {e}")

                try:
                    firebase_db.reference(f"/archived_trades_log/{order_id}").set(closed_payload)
                    print(f"🗄️ Archived closed trade {order_id} to /archived_trades_log")
                except Exception as e:
                    print(f"⚠️ Archive failed for closed trade {order_id}: {e}")

                print(f"⚠️ Skipping closed trade {order_id} for open_active_trades push")
                continue
            # ===== END NO-MAN'S-LAND GUARD =====

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
                    firebase_db.reference(f"/archived_trades_log/{order_id}").set(ghost_record)
                    # 2) Index in ghost log
                    firebase_db.reference(f"/ghost_trades_log/{order_id}").set(ghost_record)
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
        exit_ref_late = firebase_db.reference(f"/exit_orders_log/{order_id}").get()
        if exit_ref_late:
            print(f"⏭️ Skipping EXIT ticket {order_id} (late fence)")
            continue

        ref = firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}")
        try:
            existing_trade = ref.get() or {}

            # ⛔ MERGE-ONLY: if nothing exists already, do NOT create a new record here
            if not existing_trade:
                print(f"⏭️ Merge-only: skipping new order {order_id} (no existing open trade in Firebase)")
                continue

            merged_trade = {**existing_trade, **payload}
            ref.update(merged_trade)
            print(f"✅ Merged into existing open trade {order_id}")
        except Exception as e:
            print(f"❌ Failed to update /open_active_trades/{symbol}/{order_id}: {e}")

        
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