# ==============================================
# 🟩 EXIT TICKET (tx_dict) → MINIMAL FIFO CLOSE
# ==============================================
import sys
from datetime import datetime


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

    def _entry_ts(t): return t.get("entry_timestamp", "9999-12-31T23:59:59Z")
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

    # 4) Compute P&L (anchor entry vs exit fill)
    try:
        entry_price = float(anchor.get("filled_price"))
        px_exit     = float(exit_price)
        qty         = exit_qty
        if anchor.get("action", "").upper() == "BUY":
            pnl = (px_exit - entry_price) * qty
        else:  # short anchor
            pnl = (entry_price - px_exit) * qty
    except Exception as e:
        print(f"❌ PnL calc error for anchor {anchor_oid}: {e}")
        pnl = 0.0
    print(f"[INFO] P&L for {anchor_oid} via exit {exit_oid}: {pnl:.2f}")

    # 5) Close anchor (sticky entry_timestamp preserved)
    COMMISSION_FLAT = 7.02  # ✅ your confirmed flat fee
    update = {
        "exited": True,
        "trade_state": "closed",
        "contracts_remaining": 0,
        "exit_timestamp": exit_time,          # use transaction fill time
        "exit_reason": "FILLED",              # can be mapped later if you add raw reason
        "realized_pnl": pnl,
        "tiger_commissions": COMMISSION_FLAT,
        "net_pnl": pnl - COMMISSION_FLAT,
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

    print(f"[INFO] Exit ticket retained under /exit_orders_log/{exit_oid}")
    return anchor_oid
