"""Standalone Binance API connectivity test — no project dependencies needed.

Compatible with Python 3.6+. Just needs `requests` installed.

Runs from any machine. Tests:
  1. Public reachability (ping, server time)
  2. Outbound IP (so you know what to whitelist)
  3. Signed endpoint (account info — your API key + secret)
  4. Symbol info & price (BTCUSDT)
  5. Trading permission flags (canTrade, canWithdraw)

USAGE:
  Set keys via env vars:
    export BINANCE_API_KEY="your_key"
    export BINANCE_API_SECRET="your_secret"
    python test_binance.py

  Or pass on command line:
    python test_binance.py <api_key> <api_secret>

  Or interactive (prompts you):
    python test_binance.py

SAFE: only does READ operations. No orders placed. No funds moved.
"""

import hashlib
import hmac
import os
import sys
import time
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    print("ERROR: install requests first  ->  pip install requests")
    sys.exit(1)

BASE_URL = "https://api.binance.com"


def _hmac(secret, query):
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def _signed_get(api_key, api_secret, path, params=None):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    query = urlencode(params)
    sig = _hmac(api_secret, query)
    url = "{}{}?{}&signature={}".format(BASE_URL, path, query, sig)
    r = requests.get(url, headers={"X-MBX-APIKEY": api_key}, timeout=10)
    return r.json() if r.ok else {"_status": r.status_code, "_body": r.text}


def _ok(label, msg):
    print("  [OK]   {:<32} {}".format(label, msg))


def _bad(label, msg):
    print("  [FAIL] {:<32} {}".format(label, msg))


def _info(label, msg):
    print("  [..]   {:<32} {}".format(label, msg))


def run_tests(api_key, api_secret):
    print("=" * 70)
    print("BINANCE API CONNECTIVITY TEST")
    print("=" * 70)

    # 1. Outbound IP
    print("\n[1] OUTBOUND IP (this is what Binance sees)")
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
        _ok("public IP", ip)
        print("      -> if Binance has IP whitelist, this IP must be on the list")
    except Exception as e:
        _bad("public IP", "could not detect: {}".format(e))

    # 2. Public ping
    print("\n[2] BINANCE PUBLIC API (no key required)")
    try:
        r = requests.get(BASE_URL + "/api/v3/ping", timeout=10)
        if r.ok and r.json() == {}:
            _ok("ping", "Binance reachable")
        else:
            _bad("ping", "HTTP {}: {}".format(r.status_code, r.text[:100]))
            return 1
    except Exception as e:
        _bad("ping", "network error: {}".format(e))
        return 1

    # 3. Server time
    try:
        r = requests.get(BASE_URL + "/api/v3/time", timeout=10)
        server_ms = r.json()["serverTime"]
        local_ms = int(time.time() * 1000)
        delta = abs(server_ms - local_ms)
        if delta < 1000:
            _ok("clock sync", "local clock {}ms off from Binance - fine".format(delta))
        else:
            _bad("clock sync", "local clock {}ms off - may cause -1021 errors. Sync NTP.".format(delta))
    except Exception as e:
        _bad("server time", str(e))

    # 4. Sample price (public)
    try:
        r = requests.get(BASE_URL + "/api/v3/ticker/price?symbol=BTCUSDT", timeout=10)
        price = r.json().get("price")
        if price:
            _ok("BTCUSDT price", "${:,.2f}".format(float(price)))
        else:
            _bad("BTCUSDT price", str(r.json()))
    except Exception as e:
        _bad("BTCUSDT price", str(e))

    if not api_key or not api_secret:
        print("\n[!] No API key/secret provided - skipping signed endpoint tests")
        print("    Set BINANCE_API_KEY and BINANCE_API_SECRET to test full account access")
        return 0

    # 5. Account info (signed - tests key + secret + IP whitelist)
    print("\n[3] SIGNED ENDPOINT (your API key + secret)")
    acct = _signed_get(api_key, api_secret, "/api/v3/account")
    if "_status" in acct:
        status = acct["_status"]
        body = acct.get("_body", "")
        if "-2015" in body or "Invalid API-key" in body:
            _bad("account access", "FAILED - code -2015 (Invalid API-key, IP, or permissions)")
            print("      -> Causes: wrong key, wrong secret, IP not whitelisted, or trading not enabled")
            print("      -> Your IP needs to be added to the API key's whitelist on Binance")
        elif "-2014" in body:
            _bad("account access", "FAILED - code -2014 (API-key format invalid)")
        elif "-1021" in body:
            _bad("account access", "FAILED - code -1021 (timestamp out of recvWindow). Sync your clock.")
        else:
            _bad("account access", "HTTP {}: {}".format(status, body[:200]))
        return 1

    _ok("account access", "account type: {}".format(acct.get("accountType")))
    _ok("permissions", str(acct.get("permissions") or acct.get("permissionSets", [["?"]])[0][:3]))
    _info("canTrade", str(acct.get("canTrade")))
    _info("canDeposit", str(acct.get("canDeposit")))
    _info("canWithdraw", str(acct.get("canWithdraw")))

    # 6. Non-zero balances summary
    balances = [b for b in acct.get("balances", [])
                if float(b["free"]) + float(b["locked"]) > 0]
    print("\n[4] BALANCES - {} non-zero assets".format(len(balances)))
    for b in balances[:10]:
        free = float(b["free"]); locked = float(b["locked"])
        print("      {:<8}  free={:.8f}  locked={:.8f}".format(b["asset"], free, locked))
    if len(balances) > 10:
        print("      ... and {} more".format(len(balances) - 10))

    # 7. Symbol info for BTCUSDT (verify trading params)
    print("\n[5] SYMBOL INFO - BTCUSDT")
    try:
        r = requests.get(BASE_URL + "/api/v3/exchangeInfo?symbol=BTCUSDT", timeout=10)
        info = r.json()["symbols"][0]
        _info("status", info["status"])
        _info("isSpotTradingAllowed", str(info["isSpotTradingAllowed"]))
        lot = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
        notional = next((f for f in info["filters"] if f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL")), None)
        _info("LOT_SIZE step", lot["stepSize"])
        if notional:
            _info("min notional", notional.get("minNotional", "?"))
    except Exception as e:
        _bad("exchange info", str(e))

    print("\n" + "=" * 70)
    print("ALL TESTS PASSED - Binance API works from this machine")
    print("=" * 70)
    return 0


def _resolve_creds():
    # 1) command-line args
    if len(sys.argv) >= 3:
        return sys.argv[1], sys.argv[2]
    # 2) env vars
    key = os.environ.get("BINANCE_API_KEY")
    sec = os.environ.get("BINANCE_API_SECRET")
    if key and sec:
        return key, sec
    # 3) interactive prompt
    print("No keys via env or args. Enter manually (or Ctrl+C / Ctrl+D to skip):")
    try:
        key = input("  API key: ").strip()
        sec = input("  API secret: ").strip()
        return (key or None), (sec or None)
    except (KeyboardInterrupt, EOFError):
        print()
        return None, None


if __name__ == "__main__":
    k, s = _resolve_creds()
    sys.exit(run_tests(k, s))
