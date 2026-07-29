"""
Go-live readiness check — run: python check_live_ready.py
Checks every Gate 2 item live against Zerodha. All PASS = Gate 2 closed.
This script only READS account status. It places no orders, moves no money.
"""
import datetime as dt
import os
import re
import sys

GREEN, RED, YELLOW = "[PASS]", "[FAIL]", "[WARN]"
results = []

def check(name, ok, detail=""):
    tag = GREEN if ok else RED
    results.append(ok)
    print(f"{tag} {name}" + (f" — {detail}" if detail else ""))

# 1. config
API_KEY = API_SECRET = ""
if os.path.exists("config.py.txt"):
    for line in open("config.py.txt"):
        m = re.match(r'\s*(API_KEY|API_SECRET)\s*=\s*["\']([^"\']+)["\']', line)
        if m:
            if m.group(1) == "API_KEY":    API_KEY = m.group(2)
            else:                          API_SECRET = m.group(2)
check("API key + secret in config.py.txt", bool(API_KEY and API_SECRET))
if not (API_KEY and API_SECRET):
    sys.exit(1)

# password must never be stored
raw = open("config.py.txt").read().lower()
if "password" in raw and "-" in raw.split("password", 1)[1][:40] and any(
        c.isalnum() for c in raw.split("password", 1)[1][:4]):
    pass  # comment mentioning the rule is fine — only flag real-looking secrets
plain = [l for l in open("config.py.txt") if "password" in l.lower() and "never" not in l.lower() and "removed" not in l.lower()]
check("No account password stored in config", not plain,
      "REMOVE IT — bot only ever uses API key+secret" if plain else "")

# 2. token alive
from kiteconnect import KiteConnect
k = KiteConnect(api_key=API_KEY)
profile = None
try:
    k.set_access_token(open("kite_token.txt").read().strip())
    profile = k.profile()
    check("Kite token valid today", True,
          f"{profile['user_id']} {profile['user_name']}")
except Exception as e:
    check("Kite token valid today", False,
          f"{e} -> run: python kite_setup.py")

if profile:
    # 3. F&O segment
    exch = profile.get("exchanges", [])
    check("F&O segment (NFO) active", "NFO" in exch, f"enabled: {exch}")

    # 4. funds
    try:
        cash = k.margins().get("equity", {}).get("available", {}).get("cash", 0) or 0
        check("Funds >= Rs.10,000", cash >= 10000, f"available: Rs.{cash:,.0f}")
    except Exception as e:
        check("Funds >= Rs.10,000", False, str(e))

    # 5. live index quote (data plan)
    try:
        q = k.ltp(["NSE:NIFTY 50"])
        px = q["NSE:NIFTY 50"]["last_price"]
        check("Live NIFTY quote (data plan)", True, f"NIFTY {px}")
    except Exception as e:
        check("Live NIFTY quote (data plan)", False,
              f"{type(e).__name__} -> subscribe at developers.kite.trade")

    # 6. live OPTION quote on NFO (what the staircase actually needs)
    try:
        import pandas as pd
        inst = pd.DataFrame(k.instruments("NFO"))
        nifty = inst[(inst.name == "NIFTY") & (inst.instrument_type == "CE")]
        nifty = nifty.sort_values("expiry")
        sym = "NFO:" + nifty.iloc[0]["tradingsymbol"]
        q = k.ltp([sym])
        check("Live option premium quote", True,
              f"{sym} = {list(q.values())[0]['last_price']}")
    except Exception as e:
        check("Live option premium quote", False, f"{type(e).__name__}: {e}")

# 7. bot still in paper mode (must stay until ALL gates pass)
in_paper = None
for line in open("app.py"):
    m = re.match(r"\s*PAPER_TRADE\s*=\s*(True|False)", line)
    if m:
        in_paper = (m.group(1) == "True")
        break
tag = GREEN if in_paper else YELLOW
print(f"{tag} PAPER_TRADE = {not in_paper and 'False (LIVE!)' or 'True'} "
      f"(stays True until Gate 1 validation ends ~2026-07-20)")

print()
if all(results):
    print("GATE 2: ALL CHECKS PASS. Remaining gates: validation (~Jul 20),")
    print("static IP + daily token routine, 1-day dry run. See GO_LIVE_CHECKLIST.md")
else:
    print(f"GATE 2: {results.count(False)} item(s) still open. Fix the [FAIL] lines above.")
print(f"(checked {dt.datetime.now():%Y-%m-%d %H:%M})")
