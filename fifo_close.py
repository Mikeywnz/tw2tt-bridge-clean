import sys
from datetime import datetime, timezone
import gspread
from google.oauth2.service_account import Credentials
import pytz


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

# ====================================================
# 🟩 Helper: Commission by instrument
# ====================================================
def commission_for(symbol: str) -> float:
    """
    Return round-trip commission per contract.
    """
    sym3 = (symbol or "").upper()[:3]
    return {
        "MGC": 7.02,  # Micro Gold: $3.51 per side → $7.02 round-trip
        "MES": 2.64,  # Micro S&P: $1.32 per side → $2.64 round-trip
        "MCL": 4.00,  # (example) Micro Crude Oil: $2.00 per side → $4.00 round-trip
    }.get(sym3, 5.00)   # fallback default

# ==============================================
# 🟩 EXIT TICKET (tx_dict) → MINIMAL FIFO CLOSE + SHEETS LOG
# ==============================================
def handle_exit_fill_from_tx(firebase_db, tx_dict):
    """
    tx_dict example:
      {
        "status": "SUCCESS",
        "order_id": "40126…",
        "trade_type": "FLATTENING_SELL" | "FLATTENING_BUY" | "EXIT" | "MANUAL_EXIT" | "LIQUIDATION",
        "symbol": "MGC2510",
        "action": "SELL" | "BUY",
        "quantity": 1,
        "filled_price": 3374.3,
        "transaction_time": "2025-08-13T06:55:46Z",
        "source": "desktop-mac" | "mobile" | "openapi" | "tradingview" | ...
      }
    """

    # 1) Extract + sanity
    exit_oid   = str(tx_dict.get("order_id", "")).strip()
    symbol     = tx_dict.get("symbol")
    exit_price = tx_dict.get("filled_price")
    exit_time  = tx_dict.get("transaction_time") or tx_dict.get("fill_time")
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
    
    # 🔒 Idempotency guard (ticket already handled?) — SYMBOL-SCOPED
    ticket_ref = firebase_db.reference(f"/exit_orders_log/{symbol}/{exit_oid}")
    existing = ticket_ref.get() or {}
    if existing.get("_processed") or existing.get("_handled"):
        anchor_already = existing.get("anchor_id")
        print(f"[SKIP] Exit {exit_oid} already handled. anchor_id={anchor_already}")
        return anchor_already or True

    # 2) Log/Upsert exit ticket (never in open_active_trades) — SYMBOL-SCOPED
    #    Keep any provided source so Sheets can render the right "Source"
    payload = {
        "order_id": exit_oid,
        "symbol": symbol, 
        "action": exit_act,
        "filled_price": exit_price,
        "filled_qty": exit_qty,
        "fill_time": exit_time,
        "status": status,
        "trade_type": tx_dict.get("trade_type", "EXIT"),
    }
    if tx_dict.get("source"):
        payload["source"] = tx_dict.get("source")
    firebase_db.reference(f"/exit_orders_log/{symbol}").child(exit_oid).update(payload)
    print(f"[INFO] Exit ticket recorded: {exit_oid} @ {exit_price} ({exit_act})")

    # 3) Fetch oldest open anchor (FIFO by entry_timestamp)
    open_ref = firebase_db.reference(f"/open_active_trades/{symbol}")
    opens = open_ref.get() or {}
    if not opens:
        print("[WARN] No open trades to close for this exit.")
        return False

    # 🛡️ Robust UTC parser (handles Z/offset/naive)
    def _to_utc(val):
        s = (str(val) or "").strip()
        if not s:
            return datetime.utcnow().replace(tzinfo=timezone.utc)
        try:
            if s.endswith("Z"):
                return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
            if "+" in s[10:] or "-" in s[10:]:
                return datetime.fromisoformat(s).astimezone(timezone.utc)
        except Exception:
            pass
        try:
            return datetime.fromisoformat(s.replace("T", " ").split(".")[0]).replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.utcnow().replace(tzinfo=timezone.utc)

    # Normalize exit time
    exit_utc = _to_utc(exit_time)

    # Build FIFO list with normalized entry timestamps
    entries = []
    for oid, tr in opens.items():
        et = tr.get("entry_timestamp") or tr.get("transaction_time") or ""
        entries.append((oid, _to_utc(et)))
    if not entries:
        print("[WARN] No entries found under open_active_trades; cannot FIFO.")
        return False

    # Sort FIFO by true time (UTC). Choose the head.
    entries.sort(key=lambda x: x[1])
    fifo_head_oid, fifo_head_dt = entries[0]

    # Even if exit appears earlier, ALWAYS close FIFO head.
    if exit_utc < fifo_head_dt:
        print(f"[NOTE] Exit {exit_oid} appears earlier than FIFO head by time "
              f"({exit_utc.isoformat()} < {fifo_head_dt.isoformat()}) — proceeding with FIFO head anyway.")

    # Candidate must be open/eligible; if head is ineligible, pick next eligible
    def _eligible(oid_):
        tr = opens.get(oid_, {})
        return not tr.get("exited") and (tr.get("contracts_remaining", 1) > 0)

    candidate_oid = fifo_head_oid
    if not _eligible(candidate_oid):
        for oid, _dt in entries[1:]:
            if _eligible(oid):
                candidate_oid = oid
                break
        else:
            print("[WARN] No eligible open trades (all exited or zero qty).")
            return False

    anchor = dict(opens.get(candidate_oid, {}), order_id=candidate_oid)
    anchor_oid = anchor["order_id"]
    print(f"[INFO] FIFO anchor selected: {anchor_oid} (entry={anchor.get('entry_timestamp')})")

    # 4) Compute P&L in **points**, then to **dollars** via contract multiplier
    try:
        entry_price = float(anchor.get("filled_price"))
        px_exit     = float(exit_price)
        qty         = exit_qty
        if anchor.get("action", "").upper() == "BUY":
            pnl_points = (px_exit - entry_price) * qty
        else:
            pnl_points = (entry_price - px_exit) * qty
        per_point   = point_value_for(symbol)
        pnl         = pnl_points * per_point
    except Exception as e:
        print(f"❌ PnL calc error for anchor {anchor_oid}: {e}")
        pnl = 0.0

    print(f"[INFO] P&L for {anchor_oid} via exit {exit_oid}: {pnl:.2f}")

    # 5) Close anchor (sticky entry_timestamp preserved)
    commission = commission_for(symbol)
    update = {
        "exited": True,
        "trade_state": "closed",
        "contracts_remaining": 0,
        "exit_timestamp": exit_time,
        "exit_reason": "FILLED",
        "realized_pnl": round(pnl, 2),                # dollars
        "tiger_commissions": commission,
        "net_pnl": round(pnl - commission, 2),   # dollars
        "exit_order_id": exit_oid,
    }
    try:
        open_ref.child(anchor_oid).update(update)
        print(f"[INFO] Anchor {anchor_oid} marked closed.")
    except Exception as e:
        print(f"❌ Failed to update anchor {anchor_oid}: {e}")
        return False

    # 6) Archive & delete anchor (minimal + deterministic) — SYMBOL-SCOPED
    try:
        firebase_db.reference(f"/archived_trades_log/{symbol}/{anchor_oid}").set({**anchor, **update})
        firebase_db.reference(f"/open_active_trades/{symbol}/{anchor_oid}").delete()
        print(f"[INFO] Archived + deleted anchor {anchor_oid}")
    except Exception as e:
        print(f"❌ Archive/delete failed for {anchor_oid}: {e}")
        return None

    # 7) Mark exit ticket handled/processed (prevents reprocessing) — SYMBOL-SCOPED
    try:
        firebase_db.reference(f"/exit_orders_log/{symbol}/{exit_oid}").update({
            "_handled": True,
            "_processed": True,
            "anchor_id": anchor_oid,
        })
    except Exception as e:
        print(f"⚠️ Failed to mark exit ticket handled for {exit_oid}: {e}")

   #=========================================================================================
    # 8) --- Google Sheets logging (convert UTC→NZ; prefer ticket source; MANUAL note) ---
    #=========================================================================================
    try:
        # --- timestamps used for Sheets (DISPLAY ONLY) ---
        # Prefer Tiger execution time saved on the anchor (if present); else use original entry_timestamp
        entry_src_iso = str(anchor.get("transaction_time") or anchor.get("entry_timestamp") or "").strip()
        exit_ts_iso   = str(exit_time or "").strip()  # Tiger fill time (UTC Z or offset)

        entry_px     = float(anchor.get("filled_price", 0.0) or 0.0)
        exit_px      = float(exit_price or 0.0)
        trail_trig   = anchor.get("trail_trigger", "")
        trail_off    = anchor.get("trail_offset", "")
        trail_hit    = "Yes" if anchor.get("trail_hit") else "No"

        # --- UTC(Z)/offset → NZ conversion (no other assumptions) ---
        NZ_TZ = pytz.timezone("Pacific/Auckland")

        # Direct UTC → NZ conversion, but ONLY once.
        entry_dt = datetime.fromisoformat(entry_src_iso.replace("Z", "+00:00")).astimezone(NZ_TZ) if entry_src_iso else None
        exit_dt  = datetime.fromisoformat(exit_ts_iso.replace("Z", "+00:00")).astimezone(NZ_TZ) if exit_ts_iso else None

        day_date       = "'" + entry_dt.strftime("%A %d %B %Y")
        entry_time_str = "'" + entry_dt.strftime("%I:%M:%S %p")
        exit_time_str  = "'" + exit_dt.strftime("%I:%M:%S %p")

        # Duration HH:MM:SS (always positive)
        total_secs = int(abs((exit_dt - entry_dt).total_seconds()))
        hh = total_secs // 3600
        mm = (total_secs % 3600) // 60
        ss = total_secs % 60
        time_in_trade = f"{hh:02d}:{mm:02d}:{ss:02d}"

        # Flip? (Liquidation explicit; Flattening prefix handled by trade_type)
        is_liq = (tx_dict.get("trade_type") == "LIQUIDATION" or tx_dict.get("status") == "LIQUIDATION")
        trade_type_str = "LONG" if (anchor.get("action","").upper() == "BUY") else "SHORT"
        flip_str = "Liquidation" if is_liq else ("Yes" if (str(tx_dict.get("trade_type","")).startswith("FLATTENING_")) else "No")

        realized_pnl_fb = float(pnl)
        commission = commission_for(symbol)
        net_fb = realized_pnl_fb - commission

        # --- Source normalization: prefer ticket source over anchor ---
        ticket_src = (tx_dict.get("source") or "").strip()
        anchor_src = (anchor.get("source") or "").strip()
        def normalize_source(ticket_src, anchor_src, is_liq_flag):
            raw = (ticket_src or anchor_src or "").strip()
            s = raw.lower()
            if is_liq_flag or "liquidation" in s:
                return "Tiger Trade"
            if s in ("desktop", "desktop-mac", "tiger desktop"):
                return "Tiger Desktop"
            if "mobile" in s or "tiger-mobile" in s or "ios" in s or "iphone" in s or "ipad" in s or "android" in s:
                return "Tiger Mobile"
            if "opgo" in s or "openapi" in s:
                return "OpGo"
            return "OpGo"
        source_val = normalize_source(ticket_src, anchor_src, is_liq)

        notes_text = (
            "LIQUIDATION" if is_liq
            else ("MANUAL" if (tx_dict.get("trade_type") == "MANUAL_EXIT" or ticket_src.lower() == "desktop-mac") else "")
        )

        row = [
            symbol,                   # ✅ NEW COLUMN for instrument
            day_date,                 # Day Date (NZ)
            entry_time_str,           # Entry Time
            exit_time_str,            # Exit Time
            time_in_trade,            # Duration
            trade_type_str.title(),   # Long/Short
            flip_str,                 # Flip?
            entry_px,                 # Entry Price
            exit_px,                  # Exit Price
            trail_trig,
            trail_off,
            trail_hit,
            round(realized_pnl_fb, 2),
            commission,              # <-- use the variable
            round(net_fb, 2),
            anchor_oid,               # Entry ID
            exit_oid,                 # Exit ID
            source_val,               # OpGo / Tiger Desktop / Tiger Mobile
            notes_text,               # Notes
        ]

        print("[TRACE-SHEETS]", {
            "anchor_id": anchor_oid,
            "raw_entry_ts": entry_src_iso,
            "raw_exit_ts":  exit_ts_iso,
            "entry_dt_nz":  entry_dt.isoformat(),
            "exit_dt_nz":   exit_dt.isoformat(),
            "emit_entry":   entry_time_str,
            "emit_exit":    exit_time_str,
            "source_raw":   {"ticket": ticket_src, "anchor": anchor_src},
            "source_final": source_val,
            "notes":        notes_text,
        })

        sheet = get_google_sheet()
        sheet.append_row(row, value_input_option='RAW')
        print(f"✅ Logged CLOSED trade to Sheets: anchor={anchor_oid} matched_exit={exit_oid}")
    except Exception as e:
        print(f"⚠️ Sheets logging failed for anchor={anchor_oid}, exit={exit_oid}: {e}")

    return anchor_oid