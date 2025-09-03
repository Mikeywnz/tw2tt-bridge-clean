# ========================= MONITOR_TRADES_LOOP - Segment 1 ================================
import firebase_admin
from firebase_admin import credentials, initialize_app, db
import requests
import subprocess
import firebase_active_contract
import os
from execute_trade_live import place_exit_trade
import pprint
from fifo_close import handle_exit_fill_from_tx
from collections import defaultdict
import time
import pytz
from datetime import timezone
from datetime import datetime, timezone as dt_timezone, timedelta
import datetime as dt
from datetime import timezone as _utc_tz
# (UTC-only) — removed NZ local timezone usage

processed_exit_order_ids = set()
last_cleanup_timestamp = None
DRAIN_VERBOSE = False  

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


#################### ALL HELPERS FOR THIS SCRIPT ####################


# =====================================================================================
# 🟩 HELPER: trigger & Offset ATR - Lightweight ATR proxy state (EMA of tick ranges)
# =====================================================================================
# --- ATR sizing knobs ---
_ATR_ALPHA        = 0.35      # smoothing speed (higher = faster reaction)
ATR_TRIGGER_MULT  = 0.60      # trigger = 0.6 × ATR proxy
ATR_OFFSET_MULT   = 0.20      # offset = 0.2 × ATR proxy

# Floors & caps (keep values practical)
MIN_TRIGGER_FLOOR = 2.0       # never let trigger go below 2 pts
MIN_OFFSET_FLOOR  = 0.60      # never let offset go below 0.6 pts
MAX_TRIGGER_CAP   = 10.0      # cap trigger so it doesn’t blow out
MAX_OFFSET_CAP    = 4.0       # cap offset so it doesn’t blow out

# ATR state cache
_ema_absdiff = defaultdict(float)

# ==================================================================
# 🟩 HELPER - CANONICAL TIME PARSER (single source of truth for parsing)
# ==================================================================

def parse_any_ts_to_utc(s: str):
    """
    Robust parser for Tiger / our timestamps:
      - ISO with 'Z' (UTC)
      - ISO with explicit offset (+HH:MM / -HH:MM)
      - Naive ISO (assume UTC)
    Always returns tz-aware UTC datetime.
    On error, returns NOW (UTC) to avoid min/max extremes that break ordering.
    """
    s = (s or "").strip()
    if not s:
        return datetime.utcnow().replace(tzinfo=_utc_tz.utc)

    # ISO with trailing Z
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(_utc_tz.utc)
    except Exception:
        pass

    # ISO with explicit offset
    try:
        if "+" in s[10:] or "-" in s[10:]:
            return datetime.fromisoformat(s).astimezone(_utc_tz.utc)
    except Exception:
        pass

    # Fallback: treat as UTC naive ISO (strip subseconds if present)
    try:
        core = s.replace("T", " ").split(".")[0]
        return datetime.fromisoformat(core).replace(tzinfo=_utc_tz.utc)
    except Exception:
        return datetime.utcnow().replace(tzinfo=_utc_tz.utc)
    
# ==================================================================
# 🟩 HELPER - Timezone Normaliser 
# ==================================================================

def normalize_to_utc_iso(timestr: str) -> str:
    """
    Parse a Tiger/ISO timestamp string and normalize it to ISO UTC with 'Z'.
    - If input is naive (no tzinfo), assume UTC.
    - If tz-aware, convert to UTC.
    Returns: ISO string with trailing 'Z'
    """
    try:
        d = dt.datetime.fromisoformat(timestr)
    except Exception:
        try:
            d = dt.datetime.strptime(timestr, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")

    d_utc = d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)
    return d_utc.isoformat().replace("+00:00", "Z")
# ==================================================================
# 🟩 HELPER ZOMBIE CLEANUP — PER-SYMBOL, BROKER-FLAT DRIVEN
# ==================================================================
ZOMBIE_GRACE_SECONDS = 180
_flat_since_by_symbol = {}  # e.g. {"MGC2510": epoch_seconds}

def run_zombie_cleanup_if_ready(trades_list, firebase_db, current_symbol, grace_period_seconds=None):
    """
    Purge ALL /open_active_trades/<symbol> entries after the symbol has been BROKER-FLAT for >= grace.
    - "Flat" comes from /live_total_positions/by_symbol[<symbol>] == 0 (or missing).
    - Ignores timestamps, AnchorGate, etc.
    - If trades_list is empty/None, purges everything under the symbol. If provided, purges only those OIDs.
    """
    import time

    if grace_period_seconds is None:
        grace_period_seconds = ZOMBIE_GRACE_SECONDS

    # --- 1) Read broker-flat per symbol (source of truth)
    try:
        by_symbol = firebase_db.reference("/live_total_positions/by_symbol").get() or {}
        broker_net = int(by_symbol.get(current_symbol, 0) or 0)
    except Exception as e:
        print(f"[ZOMBIE] ⚠️ failed to read broker net for {current_symbol}: {e}")
        broker_net = 0  # fail-safe: treat as flat so we don't leak zombies if read fails

    is_flat = (broker_net == 0)
    now = time.time()

    if not is_flat:
        # symbol not flat → cancel timer & bail
        if _flat_since_by_symbol.pop(current_symbol, None) is not None:
            print(f"[ZOMBIE] ❌ {current_symbol} no longer flat — timer cleared")
        return

    # --- 2) Arm/advance per-symbol flat timer
    t0 = _flat_since_by_symbol.get(current_symbol)
    if t0 is None:
        _flat_since_by_symbol[current_symbol] = now
        print(f"[ZOMBIE] ⏳ {current_symbol} flat — timer started")
        return

    elapsed = now - t0
    if elapsed < grace_period_seconds:
        # still within grace; do nothing
        return

    # --- 3) Grace elapsed → purge open_active_trades/<symbol>
    open_sym_ref   = firebase_db.reference(f"/open_active_trades/{current_symbol}")
    archive_sym_ref= firebase_db.reference(f"/zombie_trades_log/{current_symbol}")

    # If caller passed an explicit list, only purge those; otherwise purge everything under the node.
    try:
        to_purge = {}
        if trades_list:
            for tr in (trades_list or []):
                oid = str(tr.get("order_id","")).strip()
                if oid:
                    to_purge[oid] = tr
        else:
            to_purge = (open_sym_ref.get() or {})

        if not to_purge:
            print(f"[ZOMBIE] ✅ {current_symbol} flat ≥{grace_period_seconds}s — nothing to purge")
            _flat_since_by_symbol.pop(current_symbol, None)
            return

        print(f"[ZOMBIE] 🧟 Purging {len(to_purge)} open entries for {current_symbol} after {int(elapsed)}s flat")
        # archive then delete
        for oid, tr in list(to_purge.items()):
            try:
                if isinstance(tr, dict):
                    # mark closed-ish for record
                    tr = {**tr, "trade_state": "closed", "is_open": False, "contracts_remaining": 0}
                archive_sym_ref.child(str(oid)).set(tr)
                open_sym_ref.child(str(oid)).delete()
            except Exception as e:
                print(f"[ZOMBIE] ❌ failed to purge {current_symbol}/{oid}: {e}")

        # reset timer for this symbol
        _flat_since_by_symbol.pop(current_symbol, None)
        print(f"[ZOMBIE] 🗑️ Purge complete for {current_symbol}")

    except Exception as e:
        print(f"[ZOMBIE] ❌ purge error for {current_symbol}: {e}")

# ======================================
# Helper: Session guard for Tokyo Chop
# =====================================

def ensure_session_guards_defaults(firebase_db):
    """
    Seed /settings/session_guards with safe defaults IF any keys are missing.
    It never overwrites existing values in Firebase.
    """
    base = "/settings/session_guards"
    snap = firebase_db.reference(base).get() or {}

    def _need(path: str, default):
        # path like "tokyo/enabled"
        parts = path.split("/")
        cur = snap
        for p in parts[:-1]:
            cur = (cur or {}).get(p, {})
        if parts[-1] not in (cur or {}):
            firebase_db.reference(f"{base}/{path}").set(default)

    # Master switch
    _need("enabled", True)

    # Tokyo (NZ local 12:00, 30 min block)
    _need("tokyo/enabled", True)
    _need("tokyo/start_local", "12:00")
    _need("tokyo/duration_min", 30)
    _need("tokyo/tz", "Pacific/Auckland")

    # New York (09:30 local, 15 min block)
    _need("new_york/enabled", True)
    _need("new_york/start_local", "09:30")
    _need("new_york/duration_min", 15)
    _need("new_york/tz", "America/New_York")

    # London (off by default)
    _need("london/enabled", False)
    _need("london/start_local", "08:00")
    _need("london/duration_min", 15)
    _need("london/tz", "Europe/London")

# ==============================================================
# 🟩 Helper: Get active session guard (Tokyo, London, New York)
# ==============================================================
def _today_local_window(start_hhmm: str, duration_min: int, tzname: str, now_utc=None):
    """Build today's [start,end) window in UTC for a given local time + duration."""
    now_utc = now_utc or datetime.now(dt_timezone.utc)
    tz = pytz.timezone(tzname or "UTC")

    # "today" in that tz
    local_now = now_utc.astimezone(tz)
    try:
        hh, mm = map(int, (start_hhmm or "00:00").split(":"))
    except Exception:
        hh, mm = 0, 0

    # today's local start at hh:mm
    start_local = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    # convert to UTC (pytz handles DST)
    start_utc = tz.localize(start_local.replace(tzinfo=None)).astimezone(dt_timezone.utc)
    end_utc   = start_utc + timedelta(minutes=int(duration_min or 0))
    return start_utc, end_utc

# Track last seen guard across calls
_last_guard = {"session": None}

def get_active_session_guard(firebase_db, now_utc=None):
    """
    Reads /settings/session_guards and returns a dict when 'now_utc' is inside a window:
      {"session": "<tokyo|new_york|london>", "start_utc": "...", "end_utc": "..."}
    """
    global _last_guard
    now_utc = now_utc or datetime.now(dt_timezone.utc)
    cfg = firebase_db.reference("/settings/session_guards").get() or {}
    if not cfg or not cfg.get("enabled", False):
        if _last_guard["session"]:
            print(f"[SESSION] { _last_guard['session'] } guard released (global disabled)")
            _last_guard["session"] = None
        return None

    for name in ("tokyo", "new_york", "london"):
        s = cfg.get(name) or {}
        if not s.get("enabled", False):
            continue
        dur = int(s.get("duration_min", 0))
        if dur <= 0:
            continue

        start_utc, end_utc = _today_local_window(
            s.get("start_local", "00:00"),
            dur,
            s.get("tz", "UTC"),
            now_utc=now_utc
        )

        if start_utc <= now_utc <= end_utc:
            # Announce on *enter*
            if _last_guard["session"] != name:
                print(f"[SESSION] {name} guard ACTIVE {start_utc} → {end_utc} UTC")
                _last_guard["session"] = name
            return {
                "session": name,
                "start_utc": start_utc.isoformat(),
                "end_utc": end_utc.isoformat(),
            }

    # Announce on *exit*
    if _last_guard["session"]:
        print(f"[SESSION] {_last_guard['session']} guard released (outside window)")
        _last_guard["session"] = None
    return None

# ==============================================
# Helper: Net position from /open_active_trades
# ==============================================
def net_position(firebase_db, symbol: str) -> int:
    """
    Net = (# BUY legs) - (# SELL legs) for *open* trades of this symbol.
    Ignores exited/closed/failed and zero-qty legs.
    """
    snap = firebase_db.reference(f"/open_active_trades/{symbol}").get() or {}
    net = 0
    for v in snap.values():
        if not isinstance(v, dict):
            continue
        if v.get("exited") or (v.get("status", "").lower() in ("closed", "failed")):
            continue
        if int(v.get("contracts_remaining", 1) or 0) <= 0:
            continue
        side = (v.get("action") or "").upper()
        if side == "BUY":
            net += 1
        elif side == "SELL":
            net -= 1
    return net


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

#==============================================
# 🟩 HELPER: LOGGING HELPERS ==================
#==============================================
_last_flags = {}
_loop_i = 0
LOG_EVERY = 5   # print heartbeat every 5 loops

def log_on_change(tag: str, value):
    """Only prints when the value changes"""
    if _last_flags.get(tag) != value:
        print(f"{tag} {value}", flush=True)
        _last_flags[tag] = value

def log_every_n(msg: str, n: int = LOG_EVERY):
    """Prints once every N calls"""
    global _loop_i
    _loop_i += 1
    if _loop_i % n == 0:
        print(msg, flush=True)

# =========================================
# 🟩 HELPER: Load Live Prices from Firebase
# =========================================
def load_live_prices():
    return db.reference("live_prices").get() or {}

# ===============================================================
# 🟩 HELPER: Both symbol and falt check in Zombie and ghost logs
# ================================================================

def _log_ids_for(dbh, path, symbol):
    try:
        node = dbh.reference(path).get() or {}
    except Exception:
        return set()
    ids = set()
    if isinstance(node, dict):
        sym_child = node.get(symbol)
        if isinstance(sym_child, dict): ids |= set(map(str, sym_child.keys()))
        for k in node.keys():
            if str(k).isdigit(): ids.add(str(k))  # flat layout
    return ids


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
    """
    Works with BOTH layouts:
      1) Flat:   /archived_trades_log/{order_id}: {...}
      2) Scoped: /archived_trades_log/{symbol}/{order_id}: {...}
    """
    try:
        node = firebase_db.reference("/archived_trades_log").get() or {}
    except Exception:
        node = {}

    # Flat layout
    if order_id in node:
        return True

    # Symbol-scoped layout
    if isinstance(node, dict):
        for child in node.values():
            if isinstance(child, dict) and order_id in child:
                return True

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


def save_open_trades(symbol, trades, grace_seconds: int = 18):

    """
    Atomic overwrite of /open_active_trades/{symbol} with a short grace period:
    - Writes all valid 'trades' you pass in.
    - Keeps any *existing* Firebase trade for this symbol if its entry_timestamp is within the last `grace_seconds`.
    - Flushes old/zombie entries.
    """
    ref = db.reference(f"/open_active_trades/{symbol}")

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
        now_utc = datetime.now(dt_timezone.utc)
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
            ts_utc = parse_any_ts_to_utc(ts)
            if ts_utc >= cutoff:
                fresh[oid] = tr  # protect very recent trade

        # 3) Atomic overwrite with protected set
        ref.set(fresh)
        print(f"✅ Open Active Trades overwritten atomically (kept {len(fresh)}; grace={grace_seconds}s)")
    except Exception as e:
        print(f"❌ Failed to save open trades to Firebase: {e}")

# =========================================================================
# 🟢 TRAILING TP & EXIT (HYBRID ATR: initial stop → ATR trail; FIFO-first)
# =========================================================================

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
            global _ema_absdiff
            raw_range = abs((current_price - ema50)) if ema50 is not None else abs(current_price - entry)
            prev = _ema_absdiff.get(symbol, raw_range)
            smoothed = (_ATR_ALPHA * raw_range) + ((1.0 - _ATR_ALPHA) * prev)
            _ema_absdiff[symbol] = smoothed

            adaptive_trigger = max(MIN_TRIGGER_FLOOR, min(MAX_TRIGGER_CAP, ATR_TRIGGER_MULT * smoothed))
            adaptive_offset  = max(MIN_OFFSET_FLOOR,  min(MAX_OFFSET_CAP,  ATR_OFFSET_MULT  * smoothed))

            print(f"[ATR] {symbol} smoothed={smoothed:.2f} trig={adaptive_trigger:.2f} off={adaptive_offset:.2f} ema50={ema50}")

            # Snapshot trail settings → Firebase (unchanged fields for visibility)
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
                trade["trail_mode"] = "ATR"
                trade["trail_trigger"] = trigger_pts
                trade["trail_offset"]  = offset_pts
                trade["trail_trigger_price"] = trigger_price
            except Exception as e:
                print(f"⚠️ Trail snapshot failed for {order_id}: {e}")

        except Exception as e:
            print(f"⚠️ ATR adapt error for {symbol}: {e}")
            adaptive_trigger = float(trigger_points)
            adaptive_offset  = float(offset_points)
            try:
                node = firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}")
                trigger_price = (entry + adaptive_trigger) if direction == 1 else (entry - adaptive_trigger)
                node.update({
                    "trail_mode": "FALLBACK",
                    "trail_trigger": adaptive_trigger,
                    "trail_offset":  adaptive_offset,
                    "trail_trigger_price": trigger_price
                })
                trade["trail_mode"] = "FALLBACK"
                trade["trail_trigger"] = adaptive_trigger
                trade["trail_offset"]  = adaptive_offset
                trade["trail_trigger_price"] = trigger_price
            except Exception as e2:
                print(f"⚠️ Fallback trail snapshot failed for {order_id}: {e2}")

        # ========================= HYBRID ATR ADDITIONS =========================
        # Use 'smoothed' as the live ATR proxy; single knob 'atr_mult' (default 2.0)
        atr_val  = float(smoothed)
        atr_mult = float(trade.get("atr_mult", 2.0))

        # 1) Initial ATR stop (immediate protection, always active pre-arm)
        if "initial_stop" not in trade or trade.get("initial_stop") is None:
            initial_stop = (entry - atr_mult * atr_val) if direction == 1 else (entry + atr_mult * atr_val)
            trade["initial_stop"] = initial_stop
            try:
                firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}").update({
                    "initial_stop": initial_stop,
                    "atr_mult": atr_mult
                })
            except Exception:
                pass

        # 2) Arming condition: once unrealized ≥ 1×ATR, flip to trailing mode
        unreal_pts = (current_price - entry) if direction == 1 else (entry - current_price)
        if not trade.get("atr_armed") and unreal_pts >= atr_val:
            trade["atr_armed"] = True
            trade["trail_peak"] = current_price  # start peak from here
            try:
                firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}").update({
                    "atr_armed": True,
                    "trail_peak": current_price
                })
            except Exception:
                pass

        # Keep existing trigger semantics for visibility (doesn't conflict)
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
                        "trail_peak": current_price
                    })
                except Exception as e:
                    print(f"❌ Failed to update trail_hit for {order_id}: {e}")
        # ======================= END HYBRID ATR ADDITIONS =======================

        exit_trigger = False  # unified exit flag

        # ---- Pre-armed phase: respect initial ATR stop
        if not trade.get("atr_armed", False):
            init_stop = float(trade.get("initial_stop"))
            # Long: exit if price <= init_stop; Short: exit if price >= init_stop
            if (direction == 1 and current_price <= init_stop) or (direction == -1 and current_price >= init_stop):
                print(f"[INFO] Initial ATR STOP hit for {order_id} at {current_price:.2f} (stop {init_stop:.2f})")
                exit_trigger = True

        # ---- Trailing phase: update peak and use ATR “breathing” leash
        if trade.get('atr_armed', False):
            prev_peak = trade.get('trail_peak', entry)
            new_peak = max(prev_peak, current_price) if direction == 1 else min(prev_peak, current_price)

            if new_peak != prev_peak:
                print(f"[DEBUG] New trail peak for {order_id}: {new_peak:.2f} (prev: {prev_peak:.2f})")
                trade['trail_peak'] = new_peak
                try:
                    firebase_db.reference(f"/open_active_trades/{symbol}").child(order_id).update({
                        "trail_peak": new_peak
                    })
                except Exception as e:
                    print(f"❌ Failed to update trail_peak for {order_id}: {e}")
            else:
                print(f"[DEBUG] Trail peak unchanged for {order_id}: {prev_peak:.2f}")

            # ATR trailing buffer replaces fixed offset
            atr_buffer = atr_mult * atr_val
            trail_stop = (trade['trail_peak'] - atr_buffer) if direction == 1 else (trade['trail_peak'] + atr_buffer)

            print(f"[DEBUG] ATR trail for {order_id}: buffer {atr_buffer:.2f} | stop {trail_stop:.2f} | price {current_price:.2f}")
            try:
                firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}").update({
                    "trail_stop_price": trail_stop,
                    "trail_offset": atr_buffer  # mirror for visibility
                })
                trade["trail_stop_price"] = trail_stop
                trade["trail_offset"] = atr_buffer
            except Exception:
                pass

            # Exit if price crosses the ATR trail
            if (direction == 1 and current_price <= trail_stop) or (direction == -1 and current_price >= trail_stop):
                print(f"[INFO] ATR Trailing EXIT condition met for {order_id}")
                exit_trigger = True

        # ---- If any exit condition is true, place exit (FIFO path unchanged)
        if exit_trigger:
            # ---- Claim to avoid duplicate exits ----
            node_ref = firebase_db.reference(f"/open_active_trades/{symbol}/{order_id}")
            try:
                current = node_ref.get() or {}
                if current.get("exit_pending"):
                    print(f"⏭️ {order_id} already claimed (exit_pending). Skipping duplicate exit.")
                    continue
                node_ref.update({"exit_pending": True})
                trade["exit_pending"] = True
            except Exception as e:
                print(f"❌ Failed to claim {order_id} (set exit_pending): {e}")
                continue

            # ---- Place exit ----
            try:
                exit_side = 'SELL' if (trade.get('action') or '').upper() == 'BUY' else 'BUY'
                result = place_exit_trade(symbol, exit_side, 1, firebase_db)

                if result.get("status") == "SUCCESS":
                    print(f"📤 Exit order placed successfully for {order_id}")
                    raw_ts = result.get("transaction_time")
                    if isinstance(raw_ts, (int, float)):
                        tx_iso = datetime.fromtimestamp(raw_ts/1000, tz=dt_timezone.utc).isoformat().replace("+00:00","Z")
                    else:
                        tx_iso = normalize_to_utc_iso(raw_ts or datetime.utcnow().isoformat())

                    tx_dict = {
                        "status": result.get("status", "SUCCESS"),
                        "order_id": str(result.get("order_id", "")),
                        "trade_type": result.get("trade_type", "EXIT"),
                        "symbol": symbol,
                        "action": result.get("action", exit_side),
                        "quantity": result.get("quantity", 1),
                        "filled_price": result.get("filled_price"),
                        "transaction_time": tx_iso,
                        "fill_time": tx_iso
                    }

                    tickets_ref = firebase_db.reference(f"/exit_orders_log/{symbol}")
                    try:
                        tickets_ref.child(tx_dict["order_id"]).set({**tx_dict, "_processed": False})
                    except Exception as e2:
                        print(f"❌ Failed to enqueue exit ticket {tx_dict.get('order_id')}: {e2}")
                        try:
                            node_ref.update({"exit_pending": False})
                            trade["exit_pending"] = False
                        except Exception:
                            pass
                    else:
                        print(f"[INFO] Exit ticket enqueued (not processed here): {tx_dict['order_id']}")
                else:
                    print(f"❌ Exit order failed for {order_id}: {result}")
                    try:
                        node_ref.update({"exit_pending": False})
                        trade["exit_pending"] = False
                    except Exception:
                        pass

            except Exception as e:
                print(f"❌ Exception placing exit for {order_id}: {e}")
                try:
                    node_ref.update({"exit_pending": False})
                    trade["exit_pending"] = False
                except Exception:
                    pass

        # Write back in-place
        active_trades[i] = trade

    return active_trades

# ========================================================
# MONITOR TRADES LOOP - CENTRAL LOOP  (multi-symbol, symbol-scoped logs)
# ========================================================

def monitor_trades():
   #print("[DEBUG] - entering monitor_trades()")

    # Ensure global/session guards once per loop (unchanged)
    ensure_session_guards_defaults(firebase_db)

    # Load trailing TP settings once (global defaults or your Firebase-backed values)
    trigger_points, offset_points = load_trailing_tp_settings()

    # Single fetch of live prices for this loop; dict of {symbol: {price:..., ema...} or number}
    prices = load_live_prices()

    # Pull ALL symbols' open trades and iterate per symbol
    try:
        all_trades_by_symbol = firebase_db.reference("/open_active_trades").get() or {}
    except Exception as e:
        print(f"❌ Failed to load /open_active_trades: {e}")
        return

    # --- AnchorGate toggle (global; default OFF if missing/error)
    try:
        ag_enabled = bool(firebase_db.reference("/settings/anchorgate_enabled").get())
    except Exception:
        ag_enabled = False
    log_on_change("[CFG] AnchorGate enabled:", ag_enabled)

    if not isinstance(all_trades_by_symbol, dict) or not all_trades_by_symbol:
        print("⚠️ No open trades found; nothing to monitor")
        return

    # Heartbeat (60s) — print a quick per-symbol price snapshot
    now = time.time()
    if not hasattr(monitor_trades, 'last_heartbeat'):
        monitor_trades.last_heartbeat = 0
    do_hb = (now - monitor_trades.last_heartbeat) >= 60
    if do_hb:
        monitor_trades.last_heartbeat = now

    # ---- Iterate per symbol ----
    for symbol, open_trades_map in all_trades_by_symbol.items():
        if not isinstance(open_trades_map, dict):  # e.g., "_heartbeat": "alive"
            continue
        if not open_trades_map:
            continue

        if do_hb:
            sym_px = (prices.get(symbol) or {}).get('price') if isinstance(prices.get(symbol), dict) else prices.get(symbol)
            print(f"🛰️  Worker alive — {symbol} price: {sym_px}")

        # 🔑 Ensure per-symbol toggles exist (harmless if already set)
        try:
            sref = firebase_db.reference(f"/settings/symbols/{symbol}")
            cfg  = sref.get() or {}
            if "gate_unlock_points" not in cfg:
                sref.update({"gate_unlock_points": 1.0})
            cref = firebase_db.reference(f"/max_open_trades/{symbol}")
            if cref.get() is None:
                cref.set(6)
        except Exception as e:
            print(f"⚠️ Settings seed skipped for {symbol}: {e}")

        # === Session guard: auto-flatten once at window start (per symbol) ===
        try:
            now_utc = datetime.now(dt_timezone.utc) # <-- fix: use imported `timezone`
            guard = get_active_session_guard(firebase_db, now_utc=now_utc)
            if guard:
                stamp_key = f"/runtime/session_guard/{guard['session']}/last_flatten_iso"
                last = firebase_db.reference(stamp_key).get()

                if not last or last < guard["start_utc"]:
                    cur = net_position(firebase_db, symbol)
                    if cur != 0:
                        side = "SELL" if cur > 0 else "BUY"
                        n = abs(cur)
                        print(f"[SESSION] Auto-flatten {n} legs ({side}) for {symbol} "
                            f"during {guard['session']} window {guard['start_utc']}→{guard['end_utc']}")

                        for _ in range(n):
                            r = place_exit_trade(symbol, side, 1, firebase_db)
                            if r and str(r.get("order_id","")).isdigit():
                                tx = {
                                    "status": r.get("status","SUCCESS"),
                                    "order_id": str(r.get("order_id","")).strip(),
                                    "trade_type": "SESSION_GUARD_EXIT",
                                    "symbol": symbol,
                                    "action": side,
                                    "quantity": 1,
                                    "filled_price": r.get("filled_price"),
                                    "transaction_time": normalize_to_utc_iso(
                                        r.get("transaction_time") or datetime.utcnow().isoformat()
                                    ),
                                    "source": "Session Guard"
                                }
                                handle_exit_fill_from_tx(firebase_db, tx)
                            else:
                                print(f"[SESSION] Exit place failed (skipping this leg): {r}")
                    else:
                        print(f"[SESSION] Net already flat for {symbol}; nothing to flatten.")

                    firebase_db.reference(stamp_key).set(guard["start_utc"])
                    print(f"[SESSION] Flattened at {guard['session']} open ({guard['start_utc']}).")
        except Exception as e:
            print(f"⚠️ Session guard flatten block failed softly for {symbol}: {e}")

        print(f"[ZOMBIE] check {symbol}: using broker flatness via /live_total_positions/by_symbol")
        # Load open trades list for this symbol; if None, the zombie helper will purge everything for the symbol
        all_trades = load_open_trades(symbol)

        # (Optional fetch if you want to inspect broker nets; not needed by the helper)
        # live_pos_data = firebase_db.reference("/live_total_positions").get() or {}
        # per_symbol = live_pos_data.get("by_symbol") or {}
        # symbol_count = int(per_symbol.get(symbol, 0))

        run_zombie_cleanup_if_ready(
            all_trades,
            firebase_db,
            symbol,
            grace_period_seconds=ZOMBIE_GRACE_SECONDS
        )
        # Filter active trades (symbol-scoped ghost/zombie logs)
        active_trades = []
        GHOST_STATUSES = {"EXPIRED", "CANCELLED", "LACK_OF_MARGIN"}
        existing_zombies = _log_ids_for(firebase_db, "/zombie_trades_log", symbol)
        existing_ghosts  = _log_ids_for(firebase_db, "/ghost_trades_log",  symbol)

        for t in all_trades:
            order_id = t.get('order_id')
            if not order_id:
                print(f"[{symbol}] ⚠️ Skipping trade with no order_id")
                continue
            if is_archived_trade(order_id, firebase_db):
                print(f"[{symbol}] ⏭️ Skipping archived trade {order_id}")
                continue
            if order_id in existing_zombies:
                print(f"[{symbol}] ⏭️ Skipping zombie trade {order_id}")
                continue
            if order_id in existing_ghosts:
                print(f"[{symbol}] ⏭️ Skipping ghost trade {order_id}")
                continue
            if t.get('exited') or t.get('status') in ['failed', 'closed']:
                print(f"[{symbol}] 🔁 Skipping exited/closed trade {order_id}")
                continue
            if not t.get('filled') and (t.get('status', '').upper() not in GHOST_STATUSES):
                print(f"[{symbol}] 🧾 Skipping {order_id} ⚠️ not filled and not a ghost trade")
                continue
            if trigger_points < 0.01 or offset_points < 0.01:
                print(f"[{symbol}] ⚠️ Skipping trade {order_id} due to invalid TP config: "
                      f"trigger={trigger_points}, buffer={offset_points}")
                continue
            active_trades.append(t)

        if not active_trades:
            print(f"[{symbol}] ⚠️ No active trades — Trade Worker happy & awake.")
            # still continue to drain any symbol-scoped exit tickets below

        # =========================
        # 🟩 EXIT PROCESSING (AnchorGate toggle)
        # =========================
        if active_trades:
            if not ag_enabled:
                # 🚪 AnchorGate OFF → plain, reliable FIFO path for this symbol
                try:
                    active_trades = process_trailing_tp_and_exits(active_trades, prices, trigger_points, offset_points)
                except Exception as e:
                    print(f"[{symbol}] ❌ FIFO process_trailing_tp_and_exits error: {e}")
            else:
                # =========================
                # 🟩 ANCHOR GATE – Sticky unlock (+config)  🟩
                # =========================
                # ---- choose FIFO anchor
                anchor = min(
                    active_trades,
                    key=lambda t: parse_any_ts_to_utc(t.get("entry_timestamp") or t.get("transaction_time") or "")
                )
                anchor_id = anchor.get("order_id")
                symbol_of_anchor = symbol  # normalized to this loop's symbol

                # ---- load per-symbol settings (auto-seed if missing)
                settings_ref = firebase_db.reference(f"/settings/symbols/{symbol_of_anchor}")
                cfg = settings_ref.get() or {}
                if "gate_unlock_points" not in cfg:
                    try:
                        settings_ref.update({"gate_unlock_points": 1.0})
                    except Exception:
                        pass
                    cfg = {"gate_unlock_points": 1.0}
                GATE_UNLOCK_PTS = float(cfg.get("gate_unlock_points", 1.0))
                HANDOFF_PAUSE_SEC = 2.0

                # ---- state across loops
                if not hasattr(monitor_trades, "_last_anchor_id"):
                    monitor_trades._last_anchor_id = None
                if not hasattr(monitor_trades, "_handoff_clear_at"):
                    monitor_trades._handoff_clear_at = 0.0
                if not hasattr(monitor_trades, "_sticky_unlock"):
                    monitor_trades._sticky_unlock = {}  # per-symbol bool

                # ---- detect anchor handoff -> short pause, reset sticky
                if anchor_id != monitor_trades._last_anchor_id:
                    print(f"[{symbol}] [INFO] Anchor handoff: {monitor_trades._last_anchor_id} → {anchor_id}")
                    monitor_trades._last_anchor_id = anchor_id
                    monitor_trades._handoff_clear_at = time.time() + HANDOFF_PAUSE_SEC
                    monitor_trades._sticky_unlock[symbol_of_anchor] = False

                handoff_active = time.time() < monitor_trades._handoff_clear_at

                # ---- compute anchor unrealized (points) vs current price
                cur_px = (prices.get(symbol) or {}).get("price") if isinstance(prices.get(symbol), dict) else prices.get(symbol)
                entry  = float(anchor.get("filled_price", 0.0) or 0.0)
                side   = (anchor.get("action") or "BUY").upper()

                if cur_px is None:
                    cur_px = entry  # last resort to avoid None math

                if side == "BUY":
                    unreal_pts = float(cur_px) - entry
                else:
                    unreal_pts = entry - float(cur_px)

                # ---- sticky unlock: once >= threshold, it stays unlocked until handoff
                sticky = monitor_trades._sticky_unlock.get(symbol_of_anchor, False)
                if (unreal_pts >= GATE_UNLOCK_PTS) and not handoff_active:
                    if not sticky:
                        print(f"[{symbol}] [GATE] Sticky UNLOCK set (+{unreal_pts:.2f}≥{GATE_UNLOCK_PTS})")
                    sticky = True
                    monitor_trades._sticky_unlock[symbol_of_anchor] = True

                # ---- apply gating to followers (anchor always unlocked)
                gate_updates = []
                for t in active_trades:
                    if t is anchor:
                        if t.get("gate_state") != "UNLOCKED":
                            t["gate_state"] = "UNLOCKED"
                            gate_updates.append((t.get("order_id"), {"gate_state": "UNLOCKED"}))
                        t["anchor_order_id"] = anchor_id
                        continue

                    was = t.get("gate_state", "PARKED")
                    t["anchor_order_id"] = anchor_id

                    if sticky and not handoff_active:
                        # followers may arm TP/trailing
                        t["gate_state"] = "UNLOCKED"
                        t.pop("skip_tp_trailing", None)
                        if was != "UNLOCKED":
                            gate_updates.append((t.get("order_id"), {"gate_state": "UNLOCKED"}))
                    else:
                        # parked until sticky unlock
                        t["gate_state"] = "PARKED"
                        t["skip_tp_trailing"] = True
                        if was != "PARKED" or "gate_state" not in t:
                            gate_updates.append((t.get("order_id"), {"gate_state": "PARKED"}))

                # ---- best-effort write of gate states (symbol-scoped)
                try:
                    ref = firebase_db.reference(f"/open_active_trades/{symbol}")
                    for oid, payload in gate_updates:
                        if oid:
                            ref.child(oid).update(payload)
                except Exception as e:
                    print(f"[{symbol}] ⚠️ Gate state update skipped: {e}")

                # ---- filter for trailing/TP processing (parked followers are skipped)
                gated_trades = [t for t in active_trades if not (t.get("gate_state") == "PARKED" and t.get("skip_tp_trailing"))]
                print(f"[{symbol}] [DEBUG] Processing {len(gated_trades)} trades post AnchorGate")

                try:
                    active_trades = process_trailing_tp_and_exits(gated_trades, prices, trigger_points, offset_points)
                except Exception as e:
                    print(f"[{symbol}] ❌ process_trailing_tp_and_exits error: {e}")
        # =========================  END EXIT PROCESSING  =========================

        # Track anchors closed in this loop so they cannot be written back
        closed_anchor_ids = set()

        # 🔽 EXIT LOGIC: drain (sorted, one per loop, immediate local delete) — SYMBOL-SCOPED
        try:
            tickets_ref = firebase_db.reference(f"/exit_orders_log/{symbol}")
            open_ref    = firebase_db.reference(f"/open_active_trades/{symbol}")
            tickets     = tickets_ref.get() or {}

            # Oldest first by fill_time/transaction_time
            if isinstance(tickets, dict):
                items = sorted(
                    tickets.items(),
                    key=lambda kv: parse_any_ts_to_utc((kv[1] or {}).get("fill_time") or (kv[1] or {}).get("transaction_time") or "")
                )
            else:
                items = []

            for tx_id, tx in items:
                # --- type check
                if not isinstance(tx, dict):
                    print(f"[{symbol}] [DRAIN] Skip {tx_id}: not a dict")
                    continue

                # --- idempotency check (either flag means already handled)
                if bool(tx.get("_processed")) or bool(tx.get("_handled")):
                    if DRAIN_VERBOSE:
                        print(f"[{symbol}] [DRAIN] Skip {tx_id}: already processed (_processed/_handled set)")
                    continue

                # --- ensure symbol (legacy/manual tickets may lack it)
                if not tx.get("symbol"):
                    tx["symbol"] = symbol
                    tickets_ref.child(tx_id).update({"symbol": symbol})
                    print(f"[{symbol}] [PATCH] Added symbol to stale exit ticket {tx_id}")

                # --- ensure source on manual desktop tickets (so Sheets shows it)
                if not tx.get("source") and tx.get("trade_type") == "MANUAL_EXIT":
                    tx["source"] = "desktop-mac"  # safe default

                # --- trace: missing time fields (just a warning; handler will still decide)
                if not (tx.get("transaction_time") or tx.get("fill_time")):
                    print(f"[{symbol}] [DRAIN] Warn {tx_id}: missing time (transaction_time/fill_time)")

                # === Option A: pre-filter stale/orphan exits so they can't close new trades ===
                try:
                    # Parse ticket time and compute age vs now
                    exit_dt = (tx.get("fill_time") or tx.get("transaction_time") or "")
                    exit_utc = parse_any_ts_to_utc(exit_dt)
                    now_utc  = datetime.now(timezone.utc)
                    age_s    = int((now_utc - exit_utc).total_seconds())

                    STALE_TICKET_WINDOW_S = 120  # 2 minutes

                    # Snapshot current opens and earliest open entry (FIFO head)
                    opens = open_ref.get() or {}
                    fifo_head_dt = None
                    if isinstance(opens, dict) and opens:
                        try:
                            fifo_head_dt = min(
                                parse_any_ts_to_utc((tr or {}).get("entry_timestamp") or (tr or {}).get("transaction_time") or "")
                                for tr in opens.values()
                            )
                        except Exception:
                            fifo_head_dt = None

                    # Quarantine rules:
                    #  A) No open trades AND ticket older than window  → quarantine
                    #  B) There are open trades AND ticket predates earliest entry by > window  → quarantine
                    stale_vs_no_opens = (not opens) and (age_s > STALE_TICKET_WINDOW_S)
                    stale_vs_fifo_head = (
                        fifo_head_dt is not None
                        and exit_utc < fifo_head_dt
                        and (fifo_head_dt - exit_utc).total_seconds() > STALE_TICKET_WINDOW_S
                    )

                    if stale_vs_no_opens or stale_vs_fifo_head:
                        # Mark handled/processed and mirror to ghost bucket with context
                        tickets_ref.child(tx_id).update({"_handled": True, "_processed": True})
                        firebase_db.reference(f"/ghost_trades_log/{symbol}/{tx_id}").set({
                            "reason": "stale_exit_ticket_pre_filter",
                            "exit_time": exit_utc.isoformat(),
                            "earliest_entry": fifo_head_dt.isoformat() if fifo_head_dt else None,
                            "age_s": age_s,
                            "payload": tx
                        })
                        print(f"[{symbol}] [PRE] Quarantined stale exit {tx_id} (age={age_s}s, "
                              f"fifo_head={fifo_head_dt.isoformat() if fifo_head_dt else 'N/A'})")
                        continue  # skip this ticket; do NOT pass to handler
                except Exception as e:
                    print(f"[{symbol}] [PRE] Quarantine check skipped for {tx_id}: {e}")
                # === end Option A pre-filter ===

                ok = handle_exit_fill_from_tx(firebase_db, tx)

                # If we got an anchor_id back, hide it locally immediately to prevent double-FIFO in this loop
                if isinstance(ok, str):
                    try:
                        open_ref.child(ok).delete()
                        closed_anchor_ids.add(ok)
                        print(f"[{symbol}] [LOCAL] Removed {ok} from open_active_trades (same-loop protection)")
                    except Exception as e:
                        print(f"[{symbol}] [LOCAL] Could not delete {ok} locally: {e}")

                # Mark processed either way (matches prior behavior)
                tickets_ref.child(tx_id).update({"_processed": True})
                print(f"[{symbol}] [INFO] Exit ticket {tx_id} processed and marked _processed")

                break  # process only ONE ticket per loop (per symbol)
        except Exception as e:
            print(f"[{symbol}] ❌ Exit ticket drain error: {e}")

        # 3B: Remove any trades from Firebase that were closed by exit tickets
        open_trades_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
        for t in list(active_trades):
            if t.get('exited') or t.get('contracts_remaining', 0) <= 0:
                oid = t.get('order_id')
                if not oid:
                    continue
                try:
                    open_trades_ref.child(oid).delete()
                    print(f"[{symbol}] 🗑️ Removed closed trade {oid} from Firebase after exit match")
                except Exception as e:
                    print(f"[{symbol}] ⚠️ Failed to delete {oid} from Firebase: {e}")

        if closed_anchor_ids:
            active_trades = [t for t in active_trades if t.get("order_id") not in closed_anchor_ids]

        # Persist only still-active trades for this symbol
        active_trades = [
            t for t in active_trades
            if t.get('contracts_remaining', 0) > 0
            and not t.get('exited')
            and t.get('status') not in ('closed', 'failed')
        ]
        save_open_trades(symbol, active_trades)
        print(f"[{symbol}] [DEBUG] Saved {len(active_trades)} active trades after processing")

    ##========END OF MAIN MONITOR TRADES LOOP FUNCTION========##

if __name__ == '__main__':
    while True:
        try:
            monitor_trades()
        except Exception as e:
            print(f"❌ ERROR in monitor_trades(): {e}")
        time.sleep(10)

# =========================  END OF SCRIPT ================================