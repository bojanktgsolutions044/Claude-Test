#!/usr/bin/env python3
"""
Probe Prospeo's NEW API (the old /domain-search was deprecated March 2026).
This tries the new search-company / search-person / enrich-person endpoints
and prints raw responses so we can build the integration precisely.
"""
import requests
import json

API_KEY = "pk_bf9cf188bea22288c22a10c7edaf1081da0d95208706531dac27ef0ca9c9ceb1"
BASE = "https://api.prospeo.io"
HEADERS = {"Content-Type": "application/json", "X-KEY": API_KEY}

COMPANY = "Americanflat"
DOMAIN = "americanflat.com"
TITLES = ["Founder", "Owner", "Co-Founder", "President",
          "VP of Marketing", "Marketing Director"]


def show(label, resp):
    print(f"\n{'='*60}\n{label}\n{'='*60}")
    print(f"Status: {resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2)[:2000])
    except Exception:
        print(resp.text[:1000])


def post(path, body):
    return requests.post(f"{BASE}{path}", headers=HEADERS, json=body, timeout=20)


# 0) Verify key works + see account info
try:
    show("0) ACCOUNT INFO", post("/account-information", {}))
except Exception as e:
    print("account-information error:", e)

# 1) SEARCH COMPANY by domain — try a few filter shapes
company_variants = [
    ("search-company domains", {"page": 1, "filters": {"company_domain": {"include": [DOMAIN]}}}),
    ("search-company website", {"page": 1, "filters": {"company_website": {"include": [DOMAIN]}}}),
    ("search-company name",    {"page": 1, "filters": {"company_name": {"include": [COMPANY]}}}),
]
for label, body in company_variants:
    try:
        r = post("/search-company", body)
        show(f"1) {label}", r)
        if r.status_code == 200:
            break
    except Exception as e:
        print(f"{label} error:", e)

# 2) SEARCH PERSON by company name + job titles
person_variants = [
    ("search-person company.names + job_title", {
        "page": 1,
        "filters": {
            "company": {"names": {"include": [COMPANY]}},
            "person_job_title": {"include": TITLES},
        },
    }),
    ("search-person company.domains + job_title", {
        "page": 1,
        "filters": {
            "company": {"domains": {"include": [DOMAIN]}},
            "person_job_title": {"include": TITLES},
        },
    }),
]
for label, body in person_variants:
    try:
        r = post("/search-person", body)
        show(f"2) {label}", r)
        if r.status_code == 200:
            break
    except Exception as e:
        print(f"{label} error:", e)

print("\n\nDONE. Copy ALL the output above and send it back.")
