import sys
from datetime import datetime, timezone, timedelta
import gspread
from google.oauth2.service_account import Credentials
import pytz

# ====================================================
# 🟩 Google Sheets setup (global)
# ====================================================
GOOGLE_SCOPE = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive'
]
GOOGLE_CREDS_FILE = "firebase_key.json"
SHEET_ID = "1TB76T6A1oWFi4T0iXdl2jfeGP1dC2MFSU-ESB3cBnVg"  # kept for reference; we open by name below
CLOSED_TRADES_FILE = "closed_trades.csv"

def get_google_sheet():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=GOOGLE_SCOPE)
    gs_client = gspread.authorize(creds)
    # If you later want to use SHEET_ID, you can: gs_client.open_by_key(SHEET_ID)
    sheet = gs_client.open("Closed Trades Journal").worksheet("demo journal")
    return sheet

# ====================================================
# 🟩 Time helpers (single source of truth)
# ====================================================
NZ_TZ = pytz.timezone("Pacific/Auckland")

def parse_any_ts_to_utc(s: str) -> datetime:
    """
    Robust parser for Tiger / our timestamps:
      - ISO with 'Z' (UTC)
      - ISO with explicit offset (+HH:MM / -HH:MM)
      - Naive ISO (assume UTC)
    Always returns tz-aware UTC datetime.
    """
    s = (s or "").strip()
    if not s:
        return datetime.utcnow().replace(tzinfo=timezone.utc)

    # ISO with trailing Z
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        pass

    # ISO with explicit offset
    try:
        if "+" in s[10:] or "-" in s[10:]:
            return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        pass

    # Fallback: treat as UTC naive ISO (strip subseconds if present)
    try:
        core = s.replace("T", " ").split(".")[0]
        return datetime.fromisoformat(core).replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.utcnow().replace(tzinfo=timezone.utc)

def to_nz_texts(utc_dt: datetime):
    """
    Convert a UTC datetime to NZT display strings.
    We prepend "'" so Sheets treats them as plain text (no auto TZ shifts).
    """
    nz = utc_dt.astimezone(NZ_TZ)
    day_date_txt = "'" + nz.strftime("%A %d %B %Y")   # e.g., 'Friday 29 August 2025
    time_txt     = "'" + nz.strftime("%I:%M:%S %p")   # e.g., '10:15:07 AM
    return day_date_txt, time_txt

def hhmmss(total_seconds: int) -> str:
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

# ====================================================
# 🟩 Dollars-per-point by instrument
# ====================================================
def point_value_for(symbol: str) -> float:
    """Dollars per 1.0 price point."""
    sym3 = (symbol or "").upper()[:3]
    return {
        "MGC": 10.0,  # Micro Gold: $10 per 1.0 (tick 0.1 = $1)
        "MES":  5.0,  # Micro S&P 500: $5 per 1.0 (tick 0.25 = $1.25)
        "MNQ":  2.0,  # Micro Nasdaq (example)
        "MCL": 10.0,  # Micro Crude: $10 per 1.0 (tick 0.01 = $0.10)
    }.get(sym3, 1.0)

# ====================================================
# 🟩 Commission by instrument (round-trip)
# ====================================================
def commission_for(symbol: str) -> float:
    sym3 = (symbol or "").upper()[:3]
    return {
        "MGC": 7.02,  # $3.51/side
        "MES": 2.64,  # $1.32/side
        "MCL": 4.00,  # example
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

    print("[TRACE-EXIT-PAYLOAD]", {
        "order_id": exit_oid, "symbol": symbol, "price": exit_price,
        "time_raw": exit_time, "action": exit_act, "qty": exit_qty, "status": status
    })
    
    # 🔒 Idempotency guard — SYMBOL-SCOPED
    ticket_ref = firebase_db.reference(f"/exit_orders_log/{symbol}/{exit_oid}")
    existing = ticket_ref.get() or {}
    if existing.get("_processed") or existing.get("_handled"):
        anchor_already = existing.get("anchor_id")
        print(f"[SKIP] Exit {exit_oid} already handled. anchor_id={anchor_already}")
        return anchor_already or True

    # 2) Log/Upsert exit ticket (separate from open_active_trades)
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

    # Normalize time parser
    def _to_utc(val):
        return parse_any_ts_to_utc(val)

    # Normalize exit time
    exit_utc = _to_utc(exit_time)

    # --- Global freshness guard for exits (only stale, no future skip) ---
    NOW_UTC = datetime.now(timezone.utc)
    skew_s = int((exit_utc - NOW_UTC).total_seconds())
    print(f"[TIME] now_utc={NOW_UTC.isoformat()} exit_utc={exit_utc.isoformat()} skew_s={skew_s}")

    STALE_WINDOW = timedelta(minutes=15)  # was 12h; now only 15 minutes
    if (NOW_UTC - exit_utc) > STALE_WINDOW:
        print(f"[SKIP] Exit {exit_oid} older than {int(STALE_WINDOW.total_seconds()/60)}m; ghosting as stale.")
        firebase_db.reference(f"/ghost_trades_log/{symbol}/{exit_oid}").set({
            "reason": "exit_too_old",
            "exit_time": exit_utc.isoformat(),
            "payload": payload
        })
        firebase_db.reference(f"/exit_orders_log/{symbol}/{exit_oid}").update({"_handled": True, "_processed": True})
        return False

    # ✅ No “future” guard anymore — if exit_utc is ahead of NOW_UTC, we still accept it

    # Build FIFO list with normalized entry timestamps
    entries = []
    for oid, tr in opens.items():
        et_raw = tr.get("entry_timestamp") or tr.get("transaction_time") or ""
        entries.append((oid, _to_utc(et_raw)))
    if not entries:
        print("[WARN] No entries found under open_active_trades; cannot FIFO.")
        return False

    # Sort FIFO by true time (UTC). Choose the head.
    entries.sort(key=lambda x: x[1])
    fifo_head_oid, fifo_head_dt = entries[0]

    # --- Age-gate stale exits that predate the earliest open entry by too much ---
    MAX_BACKFILL = 30 * 60  # 30 minutes
    if (exit_utc < fifo_head_dt) and ((fifo_head_dt - exit_utc).total_seconds() > MAX_BACKFILL):
        delta_s = int((fifo_head_dt - exit_utc).total_seconds())
        print(f"[SKIP] Exit {exit_oid} is {delta_s}s older than earliest entry "
              f"({fifo_head_dt.isoformat()}); ghosting.")
        firebase_db.reference(f"/ghost_trades_log/{symbol}/{exit_oid}").set({
            "reason": "stale_exit_before_open_entries",
            "exit_time": exit_utc.isoformat(),
            "earliest_entry": fifo_head_dt.isoformat(),
            "payload": payload
        })
        firebase_db.reference(f"/exit_orders_log/{symbol}/{exit_oid}").update({"_handled": True, "_processed": True})
        return False

    # If only slightly older, proceed but note it
    if exit_utc < fifo_head_dt:
        print(f"[NOTE] Exit {exit_oid} earlier than FIFO head by time "
              f"({exit_utc.isoformat()} < {fifo_head_dt.isoformat()}) — proceeding with FIFO head anyway.")

    # Candidate must be eligible
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

    # 4) Compute P&L in points → dollars (make debug safe)
    pnl_points = 0.0
    per_point  = point_value_for(symbol)
    try:
        entry_price = float(anchor.get("filled_price"))
        px_exit     = float(exit_price)
        qty         = exit_qty
        if anchor.get("action", "").upper() == "BUY":
            pnl_points = (px_exit - entry_price) * qty
        else:
            pnl_points = (entry_price - px_exit) * qty
        pnl = pnl_points * per_point
    except Exception as e:
        print(f"❌ PnL calc error for anchor {anchor_oid}: {e}")
        pnl = 0.0

    print(f"[INFO] P&L for {anchor_oid} via exit {exit_oid}: {pnl:.2f}  "
          f"(points={pnl_points:.4f}, $/pt={per_point})")
    
    # 4b) Decide exit_reason before building update
    is_liq   = (tx_dict.get("trade_type") == "LIQUIDATION" or tx_dict.get("status") == "LIQUIDATION")
    raw_exit = (tx_dict.get("exit_reason") or "").upper()

    if is_liq:
        exit_reason = "LIQUIDATION"
    elif tx_dict.get("trade_type") == "MANUAL_EXIT":
        exit_reason = "MANUAL"
    elif raw_exit in ("MACD", "EMA20"):
        exit_reason = raw_exit
    else:
        exit_reason = "FIFO Close"

    # 5) Close anchor (sticky entry_timestamp preserved)
    commission = commission_for(symbol)
    update = {
        "exited": True,
        "trade_state": "closed",
        "contracts_remaining": 0,
        "exit_timestamp": exit_time,
        "exit_reason": exit_reason, 
        "realized_pnl": round(pnl, 2),            # dollars
        "tiger_commissions": commission,
        "net_pnl": round(pnl - commission, 2),    # dollars
        "exit_order_id": exit_oid,
    }
    try:
        open_ref.child(anchor_oid).update(update)
        print(f"[INFO] Anchor {anchor_oid} marked closed.")
    except Exception as e:
        print(f"❌ Failed to update anchor {anchor_oid}: {e}")
        return False

    # 6) Archive & delete anchor — SYMBOL-SCOPED
    try:
        firebase_db.reference(f"/archived_trades_log/{symbol}/{anchor_oid}").set({**anchor, **update})
        firebase_db.reference(f"/open_active_trades/{symbol}/{anchor_oid}").delete()
        print(f"[INFO] Archived + deleted anchor {anchor_oid}")
    except Exception as e:
        print(f"❌ Archive/delete failed for {anchor_oid}: {e}")
        return None

    # 7) Mark exit ticket handled — SYMBOL-SCOPED
    try:
        firebase_db.reference(f"/exit_orders_log/{symbol}/{exit_oid}").update({
            "_handled": True,
            "_processed": True,
            "anchor_id": anchor_oid,
        })
    except Exception as e:
        print(f"⚠️ Failed to mark exit ticket handled for {exit_oid}: {e}")

    #=========================================================================================
    # 8) Google Sheets logging (UTC→NZ, force TEXT so Sheets can't mangle TZ)
    #=========================================================================================
    try:
        # Prefer Tiger execution time saved on the anchor; else use original entry_timestamp
        entry_src_iso = str(anchor.get("transaction_time") or anchor.get("entry_timestamp") or "").strip()
        exit_ts_iso   = str(update.get("exit_timestamp") or "").strip()  # what we wrote to FB

        entry_px   = float(anchor.get("filled_price", 0.0) or 0.0)
        exit_px    = float(exit_price or 0.0)
        trail_trig = anchor.get("trail_trigger", "")
        trail_off  = anchor.get("trail_offset", "")
        trail_hit  = "Yes" if anchor.get("trail_hit") else "No"

        # Parse to UTC and convert to NZ display TEXT
        entry_utc = parse_any_ts_to_utc(entry_src_iso)
        exit_utc  = parse_any_ts_to_utc(exit_ts_iso)

        day_date_txt, entry_time_txt = to_nz_texts(entry_utc)
        _,            exit_time_txt  = to_nz_texts(exit_utc)

        # Duration HH:MM:SS (absolute)
        dur_secs = int(abs((exit_utc - entry_utc).total_seconds()))
        time_in_trade = hhmmss(dur_secs)

        # Exit / labels
        trade_type_str = "LONG" if (anchor.get("action","").upper() == "BUY") else "SHORT"
        is_liq = (tx_dict.get("trade_type") == "LIQUIDATION" or tx_dict.get("status") == "LIQUIDATION")
        # NOTE: exit_reason is already decided earlier (section 4b). Do NOT recompute here.

        realized_pnl_fb = float(update["realized_pnl"])
        commission_amt  = commission_for(symbol)
        net_fb          = realized_pnl_fb - commission_amt

        # Source normalization (prefer ticket)
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

        # ONLY ADD: entry_reason (no other changes)
        entry_reason = str((anchor.get("entry_reason") or anchor.get("entryType") or
                            tx_dict.get("entry_reason") or tx_dict.get("entryType") or "")).strip()

        row = [
            symbol,                 # Instrument
            day_date_txt,           # Day Date (NZ)   — forced TEXT
            entry_time_txt,         # Entry Time TEXT
            exit_time_txt,          # Exit Time TEXT
            time_in_trade,          # Duration HH:MM:SS
            trade_type_str.title(), # Long/Short
            entry_reason,           # NEW — from entry alert's entryType
            exit_reason,            # Exit Reason (from section 4b)
            entry_px,               # Entry Price
            exit_px,                # Exit Price
            trail_trig,
            trail_off,
            trail_hit,
            round(realized_pnl_fb, 2),
            commission_amt,
            round(net_fb, 2),
            anchor_oid,             # Entry ID
            exit_oid,               # Exit ID
            source_val,             # Source
            notes_text,             # Notes
        ]

        print("[TRACE-SHEETS]", {
            "anchor_id": anchor_oid,
            "raw_entry_ts": entry_src_iso,
            "raw_exit_ts":  exit_ts_iso,
            "entry_utc":    entry_utc.isoformat(),
            "exit_utc":     exit_utc.isoformat(),
            "entry_nz_txt": entry_time_txt,
            "exit_nz_txt":  exit_time_txt,
            "duration":     time_in_trade,
            "entry_reason": entry_reason,   # NEW
            "exit_reason":  exit_reason,    # existing
            "pnl":          realized_pnl_fb,
            "commission":   commission_amt,
            "net":          round(net_fb, 2),
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