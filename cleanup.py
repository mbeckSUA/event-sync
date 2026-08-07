#!/usr/bin/env python3
import json, os
import requests

COURSEDOG_BASE = os.environ["COURSEDOG_BASE"]
COURSEDOG_EMAIL = os.environ["COURSEDOG_EMAIL"]
COURSEDOG_PASSWORD = os.environ["COURSEDOG_PASSWORD"]
COURSEDOG_SCHOOL = os.environ["COURSEDOG_SCHOOL"]

def get_token():
    resp = requests.post(f"{COURSEDOG_BASE}/api/v1/sessions",
        json={"email": COURSEDOG_EMAIL, "password": COURSEDOG_PASSWORD}, timeout=30)
    resp.raise_for_status()
    return resp.json()["token"]

state = json.loads(open("state.json").read())
token = get_token()
print(f"Deleting {len(state)} events...")

for uid, coursedog_id in state.items():
    resp = requests.delete(
        f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/events/{coursedog_id}",
        headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code in (200, 204):
        print(f"Deleted: {coursedog_id}")
    else:
        print(f"Failed {coursedog_id}: {resp.status_code} {resp.text}")

print("Done.")
