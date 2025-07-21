from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.common.consts import SegmentType

# === Init ===
config = TigerOpenClientConfig()
client = TradeClient(config)

# === Get positions from Tiger ===
positions = client.get_positions(
    account="21807597867063647",
    sec_type=SegmentType.FUT
)

print(f"📦 Total positions returned: {len(positions)}\n")

for pos in positions:
    print(vars(pos))
