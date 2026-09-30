"""TEMPORARY debug tool -- Student Staff Retreat is showing up as
"public event" on 2026-09-30 again, despite the INTERNAL_ONLY_TYPES /
is_public_facing() fix. Need the raw /meetings row for this specific
occurrence to see what `type`/`public`/`status` actually came back this
time. Delete this file and its workflow once done -- same pattern as
prior debug tools this session.
"""
import json
import os
import sys

import requests

BASE = os.environ.get("COURSEDOG_READONLY_BASE", "https://app.coursedog.com")
SCHOOL = os.environ.get("COURSEDOG_SCHOOL", "soka_peoplesoft_direct")
EMAIL = os.environ["COURSEDOG_READONLY_EMAIL"]
PASSWORD = os.environ["COURSEDOG_READONLY_PASSWORD"]


def get_token():
    resp = requests.post(
        f"{BASE}/api/v1/sessions",
        json={"email": EMAIL, "password": PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def main():
    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(
        f"{BASE}/api/v1/em/{SCHOOL}/meetings",
        params={"startDate": "2026-09-30", "endDate": "2026-09-30"},
        headers=headers, timeout=30,
    )
    print("status:", resp.status_code)
    data = resp.json()
    if isinstance(data, dict) and "data" in data:
        rows = data["data"]
    elif isinstance(data, dict):
        rows = list(data.values())
    else:
        rows = data

    print(f"total rows: {len(rows)}")
    matches = [m for m in rows if "retreat" in json.dumps(m).lower() and "staff" in json.dumps(m).lower()]
    print(f"matches: {len(matches)}")
    for m in matches:
        print(json.dumps(m, indent=2, default=str))
        print("---")
        eid = m.get("eventId")
        if eid:
            r2 = requests.get(f"{BASE}/api/v1/em/{SCHOOL}/events/{eid}", headers=headers, timeout=30)
            print(f"full /events/{eid} record (status {r2.status_code}):")
            print(json.dumps(r2.json(), indent=2, default=str))
            print("===")


if __name__ == "__main__":
    sys.exit(main())
