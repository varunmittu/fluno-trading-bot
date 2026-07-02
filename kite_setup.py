"""
Kite Connect setup — run this once to get your access token.
Steps:
  1. Paste your API key and secret below (from developers.kite.trade)
  2. Run: python kite_setup.py
  3. Open the printed URL in your browser and log in
  4. Copy the request_token from the redirect URL and paste it when asked
  5. Access token is saved to kite_token.txt — bot uses it from there
"""

import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "kiteconnect"])

from kiteconnect import KiteConnect
import os, re

# Credentials are read from config.py.txt (gitignored — NEVER commit that file)
API_KEY, API_SECRET = "", ""
if os.path.exists("config.py.txt"):
    for line in open("config.py.txt"):
        m = re.match(r'\s*(API_KEY|API_SECRET)\s*=\s*["\']([^"\']+)["\']', line)
        if m:
            if m.group(1) == "API_KEY":    API_KEY    = m.group(2)
            if m.group(1) == "API_SECRET": API_SECRET = m.group(2)

if not API_KEY or not API_SECRET:
    print("ERROR: API_KEY / API_SECRET not found in config.py.txt")
    print("Get them from: https://developers.kite.trade/apps")
    sys.exit(1)

kite = KiteConnect(api_key=API_KEY)

print("\n" + "="*60)
print("STEP 1 — Open this URL in your browser and log in:")
print("="*60)
print(kite.login_url())
print("="*60)
print("\nAfter login, Zerodha redirects you to a URL like:")
print("  https://127.0.0.1/?request_token=XXXXXX&action=login&status=success")
print("Copy the request_token value from that URL.\n")

if len(sys.argv) > 1:
    request_token = sys.argv[1].strip()
    print(f"Using request_token from argument: {request_token[:6]}...")
else:
    request_token = input("Paste your request_token here: ").strip()

try:
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]

    with open("kite_token.txt", "w") as f:
        f.write(access_token)

    print("\n[OK] Access token saved to kite_token.txt")
    print(f"     Token: {access_token[:10]}... (truncated for safety)")

    # Quick test — fetch profile
    kite.set_access_token(access_token)
    profile = kite.profile()
    print(f"\n[OK] Connected as: {profile['user_name']} ({profile['email']})")
    print(f"     Broker:        {profile['broker']}")
    print("\nSetup complete. You can now run the bot.")

except Exception as e:
    print(f"\n[ERROR] {e}")
    print("Common causes:")
    print("  - request_token already used (valid only once)")
    print("  - wrong API secret")
    print("  - token expired (generate a new login URL and try again)")
