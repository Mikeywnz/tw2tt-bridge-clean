import sys
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials


# ====================================================
# 🟩 Helper: Google Sheets Setup (Global)
# ====================================================

GOOGLE_SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
GOOGLE_CREDS_FILE = "firebase_key.json"
SHEET_ID = "1TB76T6A1oWFi4T0iXdl2jfeGP1dC2MFSU-ESB3cBnVg"
CLOSED_TRADES_FILE = "closed_trades.csv"

def get_google_sheet():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=GOOGLE_SCOPE)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open("Closed Trades Journal").worksheet("demo journal")
    return sheet

# ====================================================
# 🟩 Helper: to calculate X10 MCG prifit 
# ====================================================

def point_value_for(symbol: str) -> float:
    """Dollars per 1.0 price point."""
    sym3 = (symbol or "").upper()[:3]
    return {
        "MGC": 10.0,   # Micro Gold: $10 per 1.0 move (tick = 0.1 = $1)
        # add others here as needed
    }.get(sym3, 1.0)

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
    
    # 🔒 Idempotency guard (insert THIS block)
    ticket_ref = firebase_db.reference(f"/exit_orders_log/{exit_oid}")
    existing = ticket_ref.get() or {}
    # If already processed by the drain loop or by a prior call, bail early
    if existing.get("_processed") or existing.get("_handled"):
        anchor_already = existing.get("anchor_id")
        print(f"[SKIP] Exit {exit_oid} already handled. anchor_id={anchor_already}")
        return anchor_already or True

    # 2) Log/Upsert exit ticket (never in open_active_trades)
    tickets_ref = firebase_db.reference("/exit_orders_log")
    tickets_ref.child(exit_oid).update({
        "order_id": exit_oid,
        "symbol": symbol, 
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
    
    # 🛡️ Guard: prevent stale exit from killing fresh entry
    from datetime import datetime, timezone

    def _to_utc(val):
        """Robust ISO → UTC. If empty/bad, default to now()."""
        if not val:
            return datetime.utcnow().replace(tzinfo=timezone.utc)
        try:
            s = str(val).strip()
            if s.endswith("Z"):
                return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
            return datetime.fromisoformat(s).astimezone(timezone.utc)
        except Exception:
            return datetime.utcnow().replace(tzinfo=timezone.utc)

    exit_utc = _to_utc(exit_time)
    if all(_to_utc(tr.get("entry_timestamp","")) > exit_utc for tr in opens.values()):
        print(f"[SKIP] Exit {exit_oid} precedes all current entries; retry later.")
        return False

    def _entry_ts(t):
        return t.get("entry_timestamp", "9999-12-31T23:59:59Z")

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

    # 4) Compute P&L in **points**, then to **dollars** via contract multiplier
    try:
        entry_price = float(anchor.get("filled_price"))
        px_exit     = float(exit_price)
        qty         = exit_qty

        # points move (positive = profit for the trade direction)
        if anchor.get("action", "").upper() == "BUY":
            pnl_points = (px_exit - entry_price) * qty
        else:  # short anchor
            pnl_points = (entry_price - px_exit) * qty

        per_point   = point_value_for(symbol)
        pnl         = pnl_points * per_point            # <-- $$$
    except Exception as e:
        print(f"❌ PnL calc error for anchor {anchor_oid}: {e}")
        pnl = 0.0

    print(f"[INFO] P&L for {anchor_oid} via exit {exit_oid}: {pnl:.2f}")

    # 5) Close anchor (sticky entry_timestamp preserved)
    COMMISSION_FLAT = 7.02
    update = {
        "exited": True,
        "trade_state": "closed",
        "contracts_remaining": 0,
        "exit_timestamp": exit_time,
        "exit_reason": "FILLED",
        "realized_pnl": round(pnl, 2),                # dollars
        "tiger_commissions": COMMISSION_FLAT,
        "net_pnl": round(pnl - COMMISSION_FLAT, 2),   # dollars
        "exit_order_id": exit_oid,
    }
    try:
        open_ref.child(anchor_oid).update(update)
        print(f"[INFO] Anchor {anchor_oid} marked closed.")
    except Exception as e:
        print(f"❌ Failed to update anchor {anchor_oid}: {e}")
        return False

    # 6) Archive & delete anchor (minimal + deterministic)
    try:
        firebase_db.reference(f"/archived_trades_log/{anchor_oid}").set({**anchor, **update})
        firebase_db.reference(f"/open_active_trades/{symbol}/{anchor_oid}").delete()
        print(f"[INFO] Archived + deleted anchor {anchor_oid}")
    except Exception as e:
        print(f"❌ Archive/delete failed for {anchor_oid}: {e}")
        return None

    # 7) Mark exit ticket handled/processed (prevents reprocessing)
    try:
        firebase_db.reference(f"/exit_orders_log/{exit_oid}").update({
            "_handled": True,
            "_processed": True,
            "anchor_id": anchor_oid,
        })
    except Exception as e:
        print(f"⚠️ Failed to mark exit ticket handled for {exit_oid}: {e}")

    
    #============================================================================================
    # --- Log to Google Sheets logging: one row per fully-closed trade (anchor + this exit) ---
    #============================================================================================
    try:
        COMMISSION_FLAT = 7.02  # keep for row + fallback

        # Detect liquidation tickets
        is_liq = (tx_dict.get("trade_type") == "LIQUIDATION" or tx_dict.get("status") == "LIQUIDATION")

        # Pull a few fields (with safe fallbacks)
        entry_ts_iso = anchor.get("entry_timestamp") or exit_time
        # Hardened exit time fallback (prevents invalid isoformat '')
        exit_ts_iso  = (tx_dict.get("fill_time") or exit_time or datetime.utcnow().isoformat() + "Z")

        entry_px     = float(anchor.get("filled_price", 0.0) or 0.0)
        exit_px      = float(exit_price or 0.0)
        qty          = int(exit_qty or 1)
        trail_trig   = anchor.get("trail_trigger", "")
        trail_off    = anchor.get("trail_offset", "")
        trail_hit    = "Yes" if anchor.get("trail_hit") else "No"

        # --- Always write NZ time to Sheets (robust to epoch/ISO; assume naive ISO is UTC) ---
        from datetime import datetime, timezone
        import pytz
        NZ_TZ = pytz.timezone("Pacific/Auckland")

        def to_nz_dt(val):
            """Accepts epoch ms/sec or ISO (naive/with tz). Returns aware dt in NZ time."""
            # numeric epoch?
            if isinstance(val, (int, float)):
                ts = float(val)
                if ts > 1e12:  # milliseconds
                    ts /= 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(NZ_TZ)

            s = (str(val) or "").strip()
            if not s:
                return datetime.now(timezone.utc).astimezone(NZ_TZ)

            # ISO with explicit tz (Z or offset)
            try:
                if s.endswith("Z"):
                    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(NZ_TZ)
                if "+" in s[10:] or "-" in s[10:]:
                    return datetime.fromisoformat(s).astimezone(NZ_TZ)
            except Exception:
                pass

            # Naive ISO: **assume UTC**, then convert to NZ
            try:
                dt_naive = datetime.fromisoformat(s.replace("T", " ").split(".")[0])
                return dt_naive.replace(tzinfo=timezone.utc).astimezone(NZ_TZ)
            except Exception:
                # Last resort: treat as UTC "now"
                return datetime.now(timezone.utc).astimezone(NZ_TZ)

        entry_dt = to_nz_dt(entry_ts_iso)
        exit_dt  = to_nz_dt(exit_ts_iso)

        day_date       = entry_dt.strftime("%A %d %B %Y")     # e.g., Thursday 21 August 2025
        entry_time_str = entry_dt.strftime("%I:%M:%S %p")     # 12-hour with AM/PM
        exit_time_str  = exit_dt.strftime("%I:%M:%S %p")      # 12-hour with AM/PM

        # Stable HH:MM:SS duration (always positive)
        total_secs = int(abs((exit_dt - entry_dt).total_seconds()))
        hh = total_secs // 3600
        mm = (total_secs % 3600) // 60
        ss = total_secs % 60
        time_in_trade = f"{hh:02d}:{mm:02d}:{ss:02d}"

        # Flip? -> label Liquidation explicitly
        trade_type_str = "LONG" if (anchor.get("action","").upper() == "BUY") else "SHORT"
        flip_str = "Liquidation" if is_liq else ("Yes" if (tx_dict.get("trade_type","").startswith("FLATTENING_")) else "No")

        # ✅ Use the just-computed pnl from this function
        realized_pnl_fb = float(pnl)
        net_fb = realized_pnl_fb - COMMISSION_FLAT

        # --- Source normalization per your labels ---
        def normalize_source(anchor_src, ticket_src, is_liq_flag):
            cand = anchor_src or ticket_src
            if is_liq_flag and not cand:
                return "Tiger Trade"
            s = (str(cand or "")).lower()
            if "openapi" in s or "opgo" in s:
                return "OpGo"
            if "desktop" in s:
                return "Tiger Desktop"
            if "mobile" in s:
                return "Tiger Mobile"
            if "liquidation" in s or "tiger" in s:
                return "Tiger Trade"
            return "unknown"

        source_val = normalize_source(anchor.get("source"), tx_dict.get("source"), is_liq)

        # Optional notes text for the last column
        notes_text = "LIQUIDATION" if is_liq else ""

        # Build the row in your target column order
        row = [
            day_date,                 # Day Date (NZ)
            entry_time_str,           # Entry Time (NZ, 12h)
            exit_time_str,            # Exit Time (NZ, 12h)
            time_in_trade,            # Time in Trade
            trade_type_str.title(),   # Trade Type ("Long"/"Short")
            flip_str,                 # Flip? (or "Liquidation")
            entry_px,                 # Entry Price
            exit_px,                  # Exit Price
            trail_trig,               # Trail Trigger Value
            trail_off,                # Trail Offset
            trail_hit,                # Trailing Take Profit Hit (Yes/No)
            round(realized_pnl_fb, 2),# Realised PnL (from computed pnl)
            COMMISSION_FLAT,          # Tiger Commissions
            round(net_fb, 2),         # Net PNL
            anchor_oid,               # Order ID (entry / anchor)
            exit_oid,                 # FIFO Match Order ID (exit)
            source_val,               # Source (OpGo / Tiger Trade / Desktop / Mobile / unknown)
            notes_text,               # Notes ("LIQUIDATION" if so)
        ]

        # Append to Google Sheet
        sheet = get_google_sheet()
        sheet.append_row(row, value_input_option='USER_ENTERED')
        print(f"✅ Logged CLOSED trade to Sheets: anchor={anchor_oid} matched_exit={exit_oid}")
    except Exception as e:
        print(f"⚠️ Sheets logging failed for anchor={anchor_oid}, exit={exit_oid}: {e}")

    print(f"[INFO] Exit ticket retained under /exit_orders_log/{exit_oid}")
    return anchor_oid