from firebase_admin import credentials, initialize_app, db
import os

# Initialize Firebase (only do this once per process)
firebase_key_path = "/etc/secrets/firebase_key.json" if os.path.exists("/etc/secrets/firebase_key.json") else "firebase_key.json"
cred = credentials.Certificate(firebase_key_path)

initialize_app(cred, {
    'databaseURL': "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
})

def set_active_contract(symbol: str):
    ref = db.reference("/active_contract")
    ref.update({"MGC": symbol})
    print(f"Active contract set to: {symbol}")

def get_active_contract() -> str:
    ref = db.reference("/active_contract/MGC")
    symbol = ref.get()
    print(f"Current active contract: {symbol}")
    return symbol

if __name__ == "__main__":
    # Example usage:
    set_active_contract("MGC2510")  # Set active contract
    current = get_active_contract()  # Get active contract