#=========================  APP.PY - PART 1  ================================
from unittest import result
from weakref import ref
from fastapi import FastAPI, Request
import json
import os
import requests
import pytz
import random
import string
from execute_trade_live import place_entry_trade  # ✅ NEW: Import the function directly
import os
from firebase_admin import credentials, initialize_app, db
import firebase_active_contract
import firebase_admin
import time  # if not already imported
import hashlib
from fastapi import Request
from execute_trade_live import place_exit_trade
from fastapi.responses import JSONResponse
import json, hashlib, time
from fifo_close import handle_exit_fill_from_tx
import datetime as dt  # ✅ single, consistent datetime import

def normalize_to_utc_iso(timestr):
    try:
        d = dt.datetime.fromisoformat(timestr)
    except Exception:
        d = dt.datetime.strptime(timestr, "%Y-%m-%d %H:%M:%S")
    # if naive, set UTC; if tz-aware, convert to UTC
    d_utc = d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)
    return d_utc.isoformat().replace('+00:00', 'Z')

processed_exit_order_ids = set()
position_tracker = {}
app = FastAPI()
recent_payloads = {}

DEDUP_WINDOW = 10  # seconds
PRICE_FILE = "live_prices.json"
TRADE_LOG = "trade_log.json"
LOG_FILE = "app.log"

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

#################### ALL HELPERS FOR THIS SCRIPT ####################

# ==============================================================
# 🟩 Helper: Max-open-trades cap (Firebase-configurable)
# ==============================================================

def get_open_count(firebase_db, symbol: str) -> int:
    snap = firebase_db.reference(f"/open_active_trades/{symbol}").get() or {}
    count = 0
    for v in snap.values():
        if not isinstance(v, dict):
            continue
        if v.get("exited"):
            continue
        if v.get("contracts_remaining", 1) <= 0:
            continue
        if (v.get("status", "").lower() in ("closed", "failed")):
            continue
        count += 1
    return count

def get_max_open_trades(firebase_db, symbol: str) -> int:
    try:
        per_symbol = firebase_db.reference(f"/settings/symbols/{symbol}/max_open_trades").get()
        if per_symbol is not None:
            return int(per_symbol)
    except Exception:
        pass
    try:
        global_cap = firebase_db.reference("/settings/max_open_trades").get()
        if global_cap is not None:
            return int(global_cap)
    except Exception:
        pass
    return 6  # default

def record_cap_block(firebase_db, symbol: str, cap: int, open_count: int) -> None:
    try:
        firebase_db.reference(f"/rate_limits/{symbol}").update({
            "last_blocked_at": datetime.utcnow().isoformat() + "Z",
            "cap": int(cap),
            "open_count": int(open_count),
        })
    except Exception as e:
        print(f"⚠️ Failed to write rate_limits for {symbol}: {e}")

# ===============================================================
# 🟩 Helper: Safe Float, Map Source, Get exit reason helpers ===
# ===============================================================
def safe_float(val):
    try:
        return float(val)
    except:
        return 0.0

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

# ==============================================================
# 🟩 Helper: Log to file helper
# ==============================================================
def log_to_file(message: str):
    print(f"Logging: {message}")
    timestamp = dt.datetime.now(pytz.timezone("Pacific/Auckland")).isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {message}\n")

# ==============================================================
# 🟩 Helper: Load Trailing TP Settings (Firebase Admin SDK)
# ==============================================================
def load_trailing_tp_settings_admin(firebase_db):
    print("[DEBUG] Starting to load trailing TP settings from Firebase (Admin SDK)")
    try:
        ref = firebase_db.reference("/trailing_tp_settings")
        cfg = ref.get() or {}
        print(f"[DEBUG] Trailing TP config fetched: {cfg}")
        if cfg.get("enabled", False):
            trigger_points = float(cfg.get("trigger_points", 14.0))
            offset_points = float(cfg.get("offset_points", 5.0))
            print(f"[DEBUG] Trailing TP enabled with trigger_points={trigger_points}, offset_points={offset_points}")
        else:
            trigger_points = 14.0
            offset_points = 5.0
            print("[DEBUG] Trailing TP disabled; using default values")
    except Exception as e:
        print(f"[WARN] Exception loading trailing TP settings: {e}")
        trigger_points = 14.0
        offset_points = 5.0

    print(f"[DEBUG] Returning trailing TP settings: trigger_points={trigger_points}, offset_points={offset_points}")
    return trigger_points, offset_points

# ==============================================================
# 🟩 Helper:IRONCLAD TRADE CLASSIFIER (Handles all cases cleanly)
# ==============================================================
def classify_trade(symbol, action, qty, pos_tracker, fb_db):
    ttype = None  # Prevent NameError fallback

    # Fetch previous net position
    old_net = pos_tracker.get(symbol)
    if old_net is None:
        data = fb_db.reference(f"/live_total_positions/{symbol}").get() or {}
        old_net = int(data.get("position_count", 0))
        pos_tracker[symbol] = old_net

    # Determine direction
    buy = (action.upper() == "BUY")
    delta = qty if buy else -qty
    new_net = old_net + delta

    # 🧠 IRONCLAD LOGIC: 
    if old_net == 0:
        # When flat, any trade is an entry
        trade_type = "LONG_ENTRY" if buy else "SHORT_ENTRY"
        new_net = qty if buy else -qty

    elif old_net > 0:
        # Currently long
        trade_type = "LONG_ENTRY" if buy else "FLATTENING_SELL"

    elif old_net < 0:
        # Currently short
        trade_type = "FLATTENING_BUY" if buy else "SHORT_ENTRY"

    # Clamp new_net to 0 if it crosses over
    if (old_net > 0 and new_net < 0) or (old_net < 0 and new_net > 0):
        new_net = 0

    pos_tracker[symbol] = new_net
    return trade_type, new_net
 
# ==============================================================
# 🟩 Helper: Price updater
# ==============================================================
def perform_price_update(data):
        symbol = data.get("symbol")
        symbol = symbol.split("@")[0] if symbol else "UNKNOWN"
        try:
            raw_price = data.get("price", "")
            if str(raw_price).upper() in ["MARKET", "MKT"]:
                try:
                    with open(PRICE_FILE, "r") as f:
                        prices = json.load(f)
                    price = float(prices.get(data.get("symbol", ""), 0.0))
                except Exception as e:
                    log_to_file(f"Price file fallback error: {e}")
                    price = 0.0
            else:
                price = float(raw_price)
        except (ValueError, TypeError):
            log_to_file("❌ Invalid price value received")
            return {"status": "error", "reason": "invalid price"}

        try:
            with open(PRICE_FILE, "r") as f:
                prices = json.load(f)
        except FileNotFoundError:
            prices = {}

        prices[symbol] = price
        with open(PRICE_FILE, "w") as f:
            json.dump(prices, f, indent=2)

        utc_time = dt.datetime.utcnow().isoformat() + "Z"
        payload = {"price": price, "updated_at": utc_time}
        log_to_file(f"📤 Pushing price to Firebase: {symbol} → {price}")
        try:
            ref = firebase_db.reference(f"/live_prices/{symbol}")
            ref.update(payload)
            log_to_file(f"✅ Price pushed: {price}")
        except Exception as e:
            log_to_file(f"❌ Firebase price push failed: {e}")
        return {"status": "price stored"}

#################### END OF ALL HELPERS FOR THIS SCRIPT ####################

# ==
# ====================================================================================================
# ===================================== MAIN FUNCTION ==APP WEBHOOK ===================================
# ====================================================================================================

@app.post("/webhook")
async def webhook(request: Request):
    current_time = time.time()

    # ---------- read body ----------
    try:
        data = await request.json()
        print("Logging data..."); print(data); print("Finished data")
    except Exception as e:
        log_to_file(f"Failed to parse JSON: {e}")
        return JSONResponse({"status": "invalid json", "error": str(e)}, status_code=400)

    # ---------- FAST PATH: price updates (non-blocking) ----------
    if data.get("type") == "price_update":
        try:
            perform_price_update(data)
        except Exception as e:
            print(f"⚠️ price_update fast-path error: {e}")
        return JSONResponse({"ok": True}, status_code=200)

    # ---------- extract ----------
    request_symbol = data.get("symbol")
    action = (data.get("action") or "").upper()
    quantity = data.get("quantity")
    if not request_symbol or action not in {"BUY", "SELL"} or quantity is None:
        return JSONResponse({"status": "error", "message": "Missing required fields"}, status_code=400)

    # ---------- dedupe ----------
    payload_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    for k in list(recent_payloads.keys()):
        if current_time - recent_payloads[k] > DEDUP_WINDOW:
            del recent_payloads[k]
    if payload_hash in recent_payloads:
        print("⚠️ Duplicate webhook call detected; ignoring.")
        return JSONResponse({"status": "duplicate_skipped"}, status_code=200)
    recent_payloads[payload_hash] = current_time
    print(f"[LOG] Webhook received: {data}")
    log_to_file(f"Webhook received: {data}")

    # ---------- flatten-before-reverse ----------
    def net_position(firebase_db, symbol: str) -> int:
        snap = firebase_db.reference(f"/open_active_trades/{symbol}").get() or {}
        net = 0
        for v in snap.values():
            if not isinstance(v, dict):
                continue
            side = (v.get("action") or "").upper()
            if side == "BUY":
                net += 1
            elif side == "SELL":
                net -= 1
        return net

    symbol   = request_symbol
    incoming = 1 if action == "BUY" else -1
    current  = net_position(firebase_db, symbol)

    if current * incoming < 0:
        print(f"🧹 Flatten-first: net={current}, incoming={action}")
        exit_side = "SELL" if current > 0 else "BUY"

        for _ in range(abs(current)):
            r = place_exit_trade(symbol, exit_side, 1, firebase_db)

            if not r or r.get("status") != "SUCCESS" or not str(r.get("order_id", "")).isdigit():
                print(f"[WARN] exit place failed; skipping FIFO push for this leg: {r}")
                continue

            try:
                tx = {
                    "status": r.get("status", "SUCCESS"),
                    "order_id": str(r.get("order_id", "")).strip(),
                    "trade_type": r.get("trade_type", "EXIT"),
                    "symbol": symbol,
                    "action": exit_side,  # SELL to close longs / BUY to close shorts
                    "quantity": 1,
                    "filled_price": r.get("filled_price"),
                    "transaction_time": normalize_to_utc_iso(
                        r.get("transaction_time") or dt.datetime.utcnow().isoformat()
                    ),
                }
                handle_exit_fill_from_tx(firebase_db, tx)
            except Exception as e:
                print(f"[WARN] FIFO close in app.py failed softly: {e}")

        # brief wait so we don't race the reverse entry
        deadline = time.time() + 12
        while time.time() < deadline:
            if net_position(firebase_db, symbol) == 0:
                print("✅ Flat confirmed; proceeding with new entry.")
                break
            time.sleep(0.5)

        if net_position(firebase_db, symbol) != 0:
            print("⏸️ Still not flat after 12s; skipping reverse entry.")
            return JSONResponse({"status": "flatten_in_progress"}, status_code=202)
        
        # ---------- max-open-trades cap (blocks new entries only) ----------
        cap = get_max_open_trades(firebase_db, symbol)
        open_count = get_open_count(firebase_db, symbol)
        print(f"[CAP] Current cap={cap}, open_count={open_count}")
        if open_count >= cap:
            record_cap_block(firebase_db, symbol, cap, open_count)
            msg = {"status": "blocked", "reason": "max_open_trades", "cap": cap, "open_count": open_count}
            print(f"[CAP] Blocked new entry for {symbol}: open_count={open_count} cap={cap}")
            log_to_file(f"[CAP] Blocked new entry for {symbol}: open_count={open_count} cap={cap}")
            return JSONResponse(msg, status_code=202)

    # ---------- place entry ----------
    print("[DEBUG] Sending trade to execute_trade_live place_entry_trade()")
    result = place_entry_trade(request_symbol, action, quantity, firebase_db)
    print(f"[DEBUG] Received result from place_entry_trade: {result}")
    filled_price = result.get("filled_price")
    order_id = result.get("order_id")

    if not (isinstance(order_id, str) and order_id.isdigit()):
        log_to_file(f"❌ Aborting Firebase push due to invalid order_id: {order_id}")
        print(f"❌ Aborting Firebase push due to invalid order_id: {order_id}")
        return JSONResponse({"status": "error", "message": "Aborted push due to invalid order_id"}, status_code=502)

    if result.get("status") != "SUCCESS":
        try:
            firebase_db.reference(f"/ghost_trades_log/{request_symbol}/{order_id}").set(data)
            log_to_file(f"✅ Firebase ghost_trades_log updated at key: {order_id}")
        except Exception as e:
            log_to_file(f"❌ Firebase push error: {e}")
        return JSONResponse({"status": "error", "message": "Trade execution failed", "detail": result}, status_code=502)

    # trailing TP config
    try:
        trigger_points, offset_points = load_trailing_tp_settings_admin(firebase_db)
    except Exception:
        trigger_points, offset_points = 14.0, 5.0

    trade_type = (result.get("trade_type") or ("LONG_ENTRY" if action == "BUY" else "SHORT_ENTRY")).upper()
    entry_timestamp = normalize_to_utc_iso(result.get("transaction_time") or dt.datetime.utcnow().isoformat())

    new_trade = {
        "order_id": order_id,
        "symbol": symbol,
        "filled_price": filled_price or 0.0,
        "action": action,
        "trade_type": trade_type,
        "status": "FILLED",
        "contracts_remaining": data.get("contracts_remaining", quantity or 1),
        "trail_mode": "FALLBACK",
        "trail_trigger": trigger_points,
        "trail_offset": offset_points,
        "trail_hit": False,
        "trail_peak": filled_price or 0.0,
        "filled": True,
        "entry_timestamp": entry_timestamp,
        "just_executed": True,
        "exit_timestamp": None,
        "trade_state": "open",
        "quantity": data.get("quantity", 1),
        "realized_pnl": 0.0,
        "net_pnl": 0.0,
        "tiger_commissions": 0.0,
        "exit_reason": "",
        "liquidation": data.get("liquidation", False),
        "source": map_source(data.get("source", None)),
        "is_open": True,
        "is_ghost": False,
    }

    # -------- Gate state assignment --------
    try:
        # Find current anchor (oldest same-direction open trade)
        anchor = None
        opens = firebase_db.reference(f"/open_active_trades/{symbol}").get() or {}
        same_dir = [
            t for t in opens.values()
            if isinstance(t, dict)
            and (t.get("action") or "").upper() == action
            and not t.get("exited")
            and (t.get("status","").lower() not in ("closed","failed"))
        ]
        if same_dir:
            def _iso_to_utc(s):
                try:
                    s = (s or "").replace("T"," ").replace("Z","").strip()
                    d = dt.datetime.fromisoformat(s)
                    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)
                except Exception:
                    return dt.datetime.max.replace(tzinfo=dt.timezone.utc)
            anchor = min(same_dir, key=lambda t: _iso_to_utc(t.get("entry_timestamp") or t.get("transaction_time") or ""))

        # Compute anchor gate if anchor exists
        gate_state = "UNLOCKED"
        if anchor:
            anchor_entry = float(anchor.get("filled_price"))
            anchor_peak  = float(anchor.get("trail_peak", anchor_entry))
            if action == "BUY":
                anchor_gate = max(anchor_entry + (trigger_points - offset_points), anchor_peak - offset_points)
                own_trigger = (filled_price or 0.0) + trigger_points
                if own_trigger < anchor_gate:
                    gate_state = "PARKED"
                    new_trade["anchor_order_id"] = anchor.get("order_id")
                    new_trade["anchor_gate_price"] = anchor_gate
                    new_trade["skip_tp_trailing"] = True
            else:
                anchor_gate = min(anchor_entry - (trigger_points - offset_points), anchor_peak + offset_points)
                own_trigger = (filled_price or 0.0) - trigger_points
                if own_trigger > anchor_gate:
                    gate_state = "PARKED"
                    new_trade["anchor_order_id"] = anchor.get("order_id")
                    new_trade["anchor_gate_price"] = anchor_gate
                    new_trade["skip_tp_trailing"] = True

        new_trade["gate_state"] = gate_state
        print(f"[DEBUG] Assigned gate_state={gate_state} for {order_id}")
    except Exception as e:
        print(f"[WARN] Could not assign gate_state for {order_id}: {e}")

    try:
        firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}").set(new_trade)
        print(f"✅ Firebase open_active_trades updated at key: {order_id}")
    except Exception as e:
        print(f"❌ Firebase push error: {e}")

    # ---------- single return ----------
    return JSONResponse(
        {"status": result.get("status", "UNKNOWN"), "result": result},
        status_code=200 if result.get("status") == "SUCCESS" else 500
    )

# =============================== END OF SCRIPT =======================================================