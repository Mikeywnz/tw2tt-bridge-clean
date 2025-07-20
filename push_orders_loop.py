import time
from push_orders_to_firebase import push_orders_main  # see below

while True:
    try:
        print("\n🔁 Running push_orders_main()...")
        push_orders_main()
    except Exception as e:
        print(f"❌ Error: {e}")
    time.sleep(30)