import time
from push_orders_to_firebase import push_orders_main  # see below

heartbeat_counter = 0
while True:
    print("🔄 Running push_orders_main()...")
    push_orders_main()
    heartbeat_counter += 1
    if heartbeat_counter >= 6:  # Every 1 minute
        print("🫀 Order Worker Happy: Still Running...")
        heartbeat_counter = 0
    time.sleep(30)