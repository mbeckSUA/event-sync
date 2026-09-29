#!/usr/bin/env python3
"""
One-off debug script -- NOT part of the regular pipeline, not scheduled.
Compares the direct /events/{id} response against the /meetings response's
nested eventData for the same event, to find where they diverge (e.g. the
description field present on one but not the other). Delete this once the
mismatch is found and fixed.
"""
import json, os, sys
import requests

COURSEDOG_BASE = os.environ["COURSEDOG_READONLY_BASE"]
COURSEDOG_EMAIL = os.environ["COURSEDOG_READONLY_EMAIL"]
COURSEDOG_PASSWORD = os.environ["COURSEDOG_READONLY_PASSWORD"]
COURSEDOG_SCHOOL = os.environ["COURSEDOG_SCHOOL"]

EVENT_ID = sys.argv[1] if len(sys.argv) > 1 else "SukZ1g7g6pBw2j6NPvh0"
START_DATE = sys.argv[2] if len(sys.argv) > 2 else "2026-09-29"
END_DATE = sys.argv[3] if len(sys.argv) > 3 else "2026-10-01"

resp = requests.post(f"{COURSEDOG_BASE}/api/v1/sessions",
    json={"email": COURSEDOG_EMAIL, "password": COURSEDOG_PASSWORD}, timeout=30)
resp.raise_for_status()
token = resp.json()["token"]
headers = {"Authorization": f"Bearer {token}"}

print(f"=== /events/{EVENT_ID} ===")
resp = requests.get(f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/events/{EVENT_ID}",
    headers=headers, timeout=30)
resp.raise_for_status()
event_direct = resp.json()
print("top-level description present:", bool(event_direct.get("description")))
print("top-level keys:", sorted(event_direct.keys()))

print()
print(f"=== /meetings?startDate={START_DATE}&endDate={END_DATE} (filtered to eventId={EVENT_ID}) ===")
resp = requests.get(f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/meetings",
    params={"startDate": START_DATE, "endDate": END_DATE, "skip": 0, "limit": 200},
    headers=headers, timeout=30)
resp.raise_for_status()
page = resp.json()
if isinstance(page, list):
    batch = page
elif isinstance(page, dict):
    batch = page.get("data") or page.get("meetings") or list(page.values())
else:
    batch = []

matches = [m for m in batch if m.get("eventId") == EVENT_ID]
print(f"found {len(matches)} matching meeting(s) in the window")
for m in matches:
    ev = m.get("eventData") or {}
    print("eventData present:", bool(m.get("eventData")))
    print("eventData keys:", sorted(ev.keys()))
    print("eventData.description present:", bool(ev.get("description")))
    print(json.dumps(m, indent=2))
