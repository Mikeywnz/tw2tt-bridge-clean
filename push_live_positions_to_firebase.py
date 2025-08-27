# ================= PUSH_LIVE_POSITIONS_TO_FIREBASE (symbol-agnostic) =================
import os, time, pytz
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import credentials, initialize_app, db
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

NZ_TZ = pytz.timezone("Pacific/Auckland")

# --- Firebase init ---
key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
if not firebase_admin._apps:
    initialize_app(credentials.Certificate(key_path), {
        'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
    })

# --- Tiger client ---
client = TradeClient(TigerOpenClientConfig())

def _now_iso_nz():
    now = datetime.now(NZ_TZ)
    return now.strftime("%Y-%m-%d %H:%M:%S ") + now.tzname()

def push_live_positions():
    live_ref = db.reference("/live_total_positions")
    print("🟢 live-positions worker running")

    while True:
        try:
            total = 0
            by_symbol = {}

            # Pull all open positions (agnostic: futures, stocks, etc.)
            positions = []
            try:
                positions = client.get_positions(account="21807597867063647") or []
            except Exception as e:
                print(f"⚠️ get_positions() failed: {e}")

            for p in positions:
                sym = getattr(p, "symbol", None)
                qty = getattr(p, "quantity", 0) or 0
                if not sym:
                    continue
                # Treat any non-zero as “open”
                total += abs(int(qty))
                by_symbol[sym] = by_symbol.get(sym, 0) + abs(int(qty))

            payload = {
                "position_count": int(total),
                "by_symbol": by_symbol,                       # e.g. {"MGC2508": 4, "MES2509": 2}
                "last_updated": _now_iso_nz(),
                "last_updated_epoch": int(datetime.now(timezone.utc).timestamp()),
            }

            live_ref.set(payload)
            print(f"✅ position_count={total} by_symbol={by_symbol}")

        except Exception as e:
            print(f"❌ Error pushing live positions: {e}")

        time.sleep(20)

if __name__ == "__main__":
    push_live_positions()
# ================= END ======================================================