print("📁 Reading /etc/secrets/tiger_openapi_config.properties...\n")

try:
    with open("/etc/secrets/tiger_openapi_config.properties", "r") as f:
        lines = f.readlines()
        for line in lines:
            print(line.strip())
    print("\n✅ File loaded successfully.")
except Exception as e:
    print("❌ Error loading secret file:", str(e))