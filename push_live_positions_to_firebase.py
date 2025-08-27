# =========================  PUSH_LIVE_POSITIONS_TO_FIREBASE  =========================
import os, time, pytz
from datetime import datetime, timezone as dt_tz

import firebase_admin
from firebase_admin import credentials, initialize_app, db

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType

import rollover_updater  # your existing module
# import firebase_active_contract  # not needed here but fine to keep if you use elsewhere

# ---------- Firebase init ----------
FIREBASE_KEY = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_KEY)
    initialize_app(cred, {
        'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
    })

# ---------- Tiger client ----------
config = TigerOpenClientConfig()
client = TradeClient(config)

NZ_TZ = pytz.timezone("Pacific/Auckland")
ACCOUNT_ID = "21807597867063647"   # <- your account

# ---------- helpers ----------
def _now_nz_str():
    now = datetime.now(NZ_TZ)
    return now.strftime("%Y-%m-%d %H:%M:%S ") + now.tzname()

def _safe_qty(v):
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return 0

def fetch_all_positions():
    """
    Return a list of Tiger position objects across Futures + Stocks.
    Handles environments where SegmentType.STK may not exist.
    """
    out = []

    # Futures first (always supported in your env)
    try:
        fut = client.get_positions(account=ACCOUNT_ID, sec_type=SegmentType.FUT) or []
        out.extend(fut)
        print(f"📦 FUT positions: {len(fut)}", flush=True)
    except Exception as e:
        print(f"⚠️ get_positions(FUT) failed: {e}", flush=True)

    # Stocks if STK enum exists; otherwise do a broad fetch and filter
    try:
        stk = client.get_positions(account=ACCOUNT_ID, sec_type=getattr(SegmentType, "STK"))
        stk = stk or []
        out.extend(stk)
        print(f"📦 STK positions: {len(stk)}", flush=True)
    except AttributeError:
        # Some TigerOpen builds don’t expose SegmentType.STK; fall back to catch-all
        try:
            print("ℹ️ No stock enums found; using catch-all fetch and filter.", flush=True)
            allp = client.get_positions(account=ACCOUNT_ID) or []
            # keep everything; you only care about qty totals + symbol mapping
            out.extend(allp)
        except Exception as e:
            print(f"⚠️ Broad get_positions() failed: {e}", flush=True)
    except Exception as e:
        print(f"⚠️ get_positions(STK) failed: {e}", flush=True)

    return out

def build_payload(positions):
    """
    Creates the exact Firebase payload you need:
      live_total_positions = {
        position_count: <int>,               # unchanged (used by zombie logic)
        last_updated:   "<NZ time>",
        symbols: {
          "<SYMBOL>": { quantity, avg_price, sec_type }
        }
      }
    """
    symbols = {}
    total_qty = 0

    for p in positions:
        sym  = getattr(p, "symbol", None)
        qty  = _safe_qty(getattr(p, "quantity", 0))
        px   = getattr(p, "avg_price", 0.0)
        st   = str(getattr(p, "sec_type", "UNKNOWN"))

        if not sym:
            continue

        # For safety: if same symbol appears multiple times, sum quantities
        if sym not in symbols:
            symbols[sym] = {"quantity": 0, "avg_price": float(px) if px is not None else 0.0, "sec_type": st}
        symbols[sym]["quantity"] += qty

        total_qty += qty

    payload = {
        "position_count": total_qty,     # <-- matches your original logic (don’t change to abs-sum)
        "last_updated": _now_nz_str(),
        "symbols": symbols if symbols else {}  # keep path stable
    }
    return payload

# ---------- main worker ----------
def push_live_positions():
    live_ref = db.reference("/live_total_positions")
    last_rollover_date = None

    print("🟢 live positions worker started", flush=True)

    while True:
        try:
            # Daily rollover (NZ)
            nz_date = datetime.now(NZ_TZ).date()
            if last_rollover_date != nz_date:
                try:
                    print(f"⏰ Rollover check for {nz_date}", flush=True)
                    rollover_updater.main()
                except Exception as e:
                    print(f"⚠️ Rollover updater failed: {e}", flush=True)
                last_rollover_date = nz_date

            # Pull, build, push
            positions = fetch_all_positions()
            payload   = build_payload(positions)

            live_ref.set(payload)
            print(f"✅ Pushed position_count={payload['position_count']} | symbols={len(payload['symbols'])} @ {payload['last_updated']}", flush=True)

            # Keep path alive (belt-and-braces)
            if not (live_ref.get() or {}):
                live_ref.child("_heartbeat").set("alive")

        except Exception as e:
            print(f"❌ Error pushing live positions: {e}", flush=True)

        time.sleep(20)

# ---------- entry ----------
if __name__ == "__main__":
    push_live_positions()
# =========================  END  =========================