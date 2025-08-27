# =========================  PUSH_LIVE_POSITIONS_TO_FIREBASE  =========================
import os, time
from datetime import datetime
import pytz

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType

import rollover_updater  # keep
import firebase_admin
from firebase_admin import credentials, initialize_app, db

# --- Firebase ---
FIREBASE_KEY = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_KEY)
        initialize_app(cred, {
            "databaseURL": "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
        })
except Exception as e:
    print(f"❌ Firebase init failed: {e}", flush=True)

# --- Tiger ---
config = TigerOpenClientConfig()
client = TradeClient(config)
ACCOUNT_ID = "21807597867063647"

NZ_TZ = pytz.timezone("Pacific/Auckland")

# ---------- helpers ----------
def _existing_stock_segments():
    """Return a list of stock-like SegmentType enums that actually exist in this package."""
    candidates = ["STK", "STOCK", "EQUITY", "US_STK", "US_STOCK", "CN_STK", "HK_STK"]
    return [getattr(SegmentType, name) for name in candidates if hasattr(SegmentType, name)]

def _safe_get_positions(sec_type_enum_or_none):
    """Call Tiger get_positions safely; return [] on error."""
    try:
        if sec_type_enum_or_none is None:
            return client.get_positions(account=ACCOUNT_ID) or []
        return client.get_positions(account=ACCOUNT_ID, sec_type=sec_type_enum_or_none) or []
    except Exception as e:
        print(f"⚠️ get_positions failed for sec_type={sec_type_enum_or_none}: {e}", flush=True)
        return []

def _norm_num(v, kind=float):
    try:
        return kind(v)
    except Exception:
        return 0.0 if kind is float else 0

def _norm_str(v):
    try:
        s = str(v)
        # Some Tiger enums stringify like SegmentType.FUT; keep the tail part if present
        return s.split(".")[-1]
    except Exception:
        return "UNKNOWN"

# ---------- main worker ----------
def push_live_positions():
    live_ref = db.reference("/live_total_positions")
    print("🟢 push_live_positions worker started", flush=True)

    last_rollover_date = None

    while True:
        try:
            # Daily rollover check (NZ)
            now_nz_date = datetime.now(NZ_TZ).date()
            if last_rollover_date != now_nz_date:
                try:
                    print(f"⏰ Rollover check for {now_nz_date}", flush=True)
                    rollover_updater.main()
                except Exception as re:
                    print(f"⚠️ Rollover updater failed: {re}", flush=True)
                last_rollover_date = now_nz_date

            all_positions = []

            # Futures (always available in your build)
            fut_enum = getattr(SegmentType, "FUT", None)
            if fut_enum is not None:
                fut_pos = _safe_get_positions(fut_enum)
                print(f"📦 FUT positions: {len(fut_pos)}", flush=True)
                all_positions.extend(fut_pos)
            else:
                print("⚠️ SegmentType.FUT not found in this Tiger build", flush=True)

            # Stocks: try any enums that exist; if none, we’ll catch-all below
            stock_enums = _existing_stock_segments()
            if stock_enums:
                total = 0
                for en in stock_enums:
                    ps = _safe_get_positions(en)
                    total += len(ps)
                    all_positions.extend(ps)
                print(f"📦 STOCK-like positions via enums { [e for e in stock_enums] }: {total}", flush=True)
            else:
                print("ℹ️ No stock enums found; will use catch-all fetch and filter.", flush=True)

            # Catch-all: get everything (covers builds where sec_type argument is limited)
            catch_all = _safe_get_positions(None)
            if catch_all:
                print(f"📦 Catch-all positions: {len(catch_all)}", flush=True)
                all_positions.extend(catch_all)

            # De-dupe by (symbol, sec_type)
            seen = set()
            unique_positions = []
            for p in all_positions:
                sym = getattr(p, "symbol", None)
                st  = _norm_str(getattr(p, "sec_type", "UNKNOWN"))
                key = (sym, st)
                if sym and key not in seen:
                    unique_positions.append(p)
                    seen.add(key)

            # Build payload
            updates = {}
            for pos in unique_positions:
                symbol  = getattr(pos, "symbol", None)
                qty     = _norm_num(getattr(pos, "quantity", 0), int)
                avg_px  = _norm_num(getattr(pos, "avg_price", 0.0), float)
                sec_str = _norm_str(getattr(pos, "sec_type", "UNKNOWN"))
                if not symbol:
                    continue
                updates[symbol] = {
                    "quantity": qty,
                    "avg_price": avg_px,
                    "sec_type": sec_str,
                }

            now_nz = datetime.now(NZ_TZ)
            updates["last_updated"] = now_nz.strftime("%Y-%m-%d %H:%M:%S ") + (now_nz.tzname() or "NZ")

            # Push
            live_ref.set(updates)
            print(f"✅ Pushed {len(unique_positions)} positions at {updates['last_updated']}", flush=True)

            # Keep path alive if empty
            if not updates or (len(updates) == 1 and "last_updated" in updates):
                live_ref.child("_heartbeat").set("alive")

        except Exception as e:
            print(f"❌ Error pushing live positions: {e}", flush=True)

        time.sleep(20)

# ---------- entry ----------
if __name__ == "__main__":
    push_live_positions()