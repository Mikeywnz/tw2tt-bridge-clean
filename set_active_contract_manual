import firebase_admin
from firebase_admin import credentials, db
import os

# Initialize Firebase only once
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
cred = credentials.Certificate(firebase_key_path)

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred, {
        'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
    })

def set_active_contract(symbol: str):
    ref = db.reference("/active_contract")
    ref.update({"MGC": symbol})
    print(f"✅ Manually set active contract to {symbol}")

if __name__ == "__main__":
    # Change this to the contract you want to activate
    set_active_contract("MGC2510")