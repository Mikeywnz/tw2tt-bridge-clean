  if "rejected" in str(result).lower():
            log_to_file("⚠️ Trade rejected — logging ghost entry.")
            try:
                day_date = datetime.now(pytz.timezone("Pacific/Auckland")).strftime("%A %d %B %Y")

                sheet.append_row([
                    day_date,
                    symbol,
                    "REJECTED",     # direction
                    0.0,            # entry_price
                    0.0,            # exit_price
                    0.0,            # pnl_dollars
                    "ghost_trade",  # reason_for_exit
                    entry_timestamp,
                    "",             # exit_time
                    False,          # trail_triggered
                    trade_id        # Tiger Order ID (even if it failed)
                ])
                log_to_file("Ghost trade logged to Sheets.")
            except Exception as e:
                log_to_file(f"❌ Ghost sheet log failed: {e}")
            return {"status": "trade not filled"}