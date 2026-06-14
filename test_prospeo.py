#!/usr/bin/env python3
"""Quick test to debug Prospeo API response."""
import requests
import json

API_KEY = "pk_bf9cf188bea22288c22a10c7edaf1081da0d95208706531dac27ef0ca9c9ceb1"
DOMAIN = "americanflat.com"

headers = {
    "Content-Type": "application/json",
    "X-KEY": API_KEY,
}

print("Testing Prospeo API...\n")

# Try every possible format
tests = [
    ("POST /domain-search {url}",        "POST", "https://api.prospeo.io/domain-search",  {"url": DOMAIN}),
    ("POST /domain-search {domain}",     "POST", "https://api.prospeo.io/domain-search",  {"domain": DOMAIN}),
    ("POST /company-search {url}",       "POST", "https://api.prospeo.io/company-search", {"url": DOMAIN}),
    ("POST /company-search {domain}",    "POST", "https://api.prospeo.io/company-search", {"domain": DOMAIN}),
    ("GET  /domain-search ?url=",        "GET",  f"https://api.prospeo.io/domain-search?url={DOMAIN}", None),
]

for label, method, url, body in tests:
    try:
        if method == "POST":
            r = requests.post(url, headers=headers, json=body, timeout=10)
        else:
            r = requests.get(url, headers=headers, timeout=10)

        print(f"[{label}]")
        print(f"  Status : {r.status_code}")
        try:
            print(f"  Body   : {json.dumps(r.json(), indent=2)[:500]}")
        except Exception:
            print(f"  Body   : {r.text[:300]}")
        print()

        if r.status_code == 200:
            print(">>> SUCCESS with format:", label)
            break
    except Exception as e:
        print(f"[{label}] ERROR: {e}\n")
