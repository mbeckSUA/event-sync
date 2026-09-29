#!/usr/bin/env python3
"""
One-off debug script -- NOT part of the regular pipeline, not scheduled.
Dumps the raw Coursedog event record for a single event ID so we can see
exactly which field holds description text for event types where our
build scripts come up blank (e.g. "Academic Events and Reservations
(Undergraduate)"). Delete this once the mismatch is found and fixed.
"""
import json, os, sys
import requests

COURSEDOG_BASE = os.environ["COURSEDOG_READONLY_BASE"]
COURSEDOG_EMAIL = os.environ["COURSEDOG_READONLY_EMAIL"]
COURSEDOG_PASSWORD = os.environ["COURSEDOG_READONLY_PASSWORD"]
COURSEDOG_SCHOOL = os.environ["COURSEDOG_SCHOOL"]

EVENT_ID = sys.argv[1] if len(sys.argv) > 1 else "SukZ1g7g6pBw2j6NPvh0"

resp = requests.post(f"{COURSEDOG_BASE}/api/v1/sessions",
    json={"email": COURSEDOG_EMAIL, "password": COURSEDOG_PASSWORD}, timeout=30)
resp.raise_for_status()
token = resp.json()["token"]

resp = requests.get(f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/events/{EVENT_ID}",
    headers={"Authorization": f"Bearer {token}"}, timeout=30)
resp.raise_for_status()
print(json.dumps(resp.json(), indent=2))
