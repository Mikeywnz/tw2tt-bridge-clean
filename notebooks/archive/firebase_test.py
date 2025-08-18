import firebase_admin
from firebase_admin import credentials, db

# Load credentials from renamed key file
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred, {
    "databaseURL": "https://tw2tt-firebase-default-rtdb.asia-southeast1.firebasedatabase.app/"
})

# Write a simple test value to Firebase
ref = db.reference("test_connection")
ref.set({
    "status": "✅ Firebase connection successful"
})

print("✅ Test data written to Firebase Realtime DB.")