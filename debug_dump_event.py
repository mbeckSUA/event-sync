#!/usr/bin/env python3
"""
One-off debug script -- NOT part of the regular pipeline, not scheduled.
Checks whether a bulk /events listing exists (like /rooms, /organizations)
that returns full event records including description, so we could use it
to backfill the description field that /meetings' nested eventData is
missing for some events. Delete this once resolved.
"""
import json, os
import requests

COURSEDOG_BASE = os.environ["COURSEDOG_READONLY_BASE"]
COURSEDOG_EMAIL = os.environ["COURSEDOG_READONLY_EMAIL"]
COURSEDOG_PASSWORD = os.environ["COURSEDOG_READONLY_PASSWORD"]
COURSEDOG_SCHOOL = os.environ["COURSEDOG_SCHOOL"]

resp = requests.post(f"{COURSEDOG_BASE}/api/v1/sessions",
    json={"email": COURSEDOG_EMAIL, "password": COURSEDOG_PASSWORD}, timeout=30)
resp.raise_for_status()
token = resp.json()["token"]
headers = {"Authorization": f"Bearer {token}"}

for path, params in [
    ("events", {}),
    ("events", {"skip": 0, "limit": 5}),
    ("events/search/Symposium", {}),
]:
    print(f"=== GET /{path} params={params} ===")
    try:
        resp = requests.get(f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/{path}",
            params=params, headers=headers, timeout=30)
        print("status:", resp.status_code)
        if resp.ok:
            data = resp.json()
            if isinstance(data, list):
                print(f"list of {len(data)} items")
                if data:
                    print("first item keys:", sorted(data[0].keys()))
                    print("first item has description:", "description" in data[0], "-- value present:", bool(data[0].get("description")))
            elif isinstance(data, dict):
                print(f"dict with {len(data)} top-level keys")
                print("keys sample:", list(data.keys())[:10])
        else:
            print("body:", resp.text[:500])
    except Exception as e:
        print("error:", e)
    print()
