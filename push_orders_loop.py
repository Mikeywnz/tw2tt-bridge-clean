import time
from push_orders_to_firebase import push_orders_main  # Make sure this matches your file structure

while True:
    print("🔄 Running push_orders_main()...")
    push_orders_main()
    print("🔁 Worker Happy: Still Running...")
    time.sleep(30)