import os

print("🔐 Checking Render Environment Variables...\n")

print("TIGER_ID:", os.getenv("TIGER_ID"))
print("TIGER_ACCOUNT:", os.getenv("TIGER_ACCOUNT"))
print("ENV:", os.getenv("ENV"))
print("LANG:", os.getenv("LANG"))
print("PRIVATE_KEY snippet:", os.getenv("PRIVATE_KEY")[:100], "...")  # Show first 100 chars only

print("\n✅ Done. This does NOT affect production.")