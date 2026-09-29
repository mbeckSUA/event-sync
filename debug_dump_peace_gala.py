"""TEMPORARY debug tool -- dump raw Coursedog data for the Peace Gala event
so we can see how setup/teardown days are actually represented, before
designing a real fix for the security report over-flagging them. Delete
this file and its workflow once the investigation is done (same pattern as
the earlier debug_dump_event.py used for the description-field bug).
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

    print("=" * 70)
    print("1. Searching events by name for 'Peace Gala'")
    print("=" * 70)
    resp = requests.get(
        f"{BASE}/api/v1/em/{SCHOOL}/events/search/Peace Gala",
        headers=headers, timeout=30,
    )
    print("status:", resp.status_code)
    try:
        data = resp.json()
    except Exception:
        print(resp.text[:3000])
        data = None

    event_ids = []
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], list):
            items = data["data"]
        else:
            items = list(data.values())
    elif isinstance(data, list):
        items = data
    else:
        items = []

    for item in items:
        eid = item.get("_id") or item.get("id")
        print(f"  - id={eid}  name={item.get('name')}  type={item.get('type')}  org={item.get('organization')}")
        if eid:
            event_ids.append(eid)

    print()
    print("=" * 70)
    print("2. Full /events/{id} record for each match")
    print("=" * 70)
    for eid in event_ids:
        resp = requests.get(
            f"{BASE}/api/v1/em/{SCHOOL}/events/{eid}",
            headers=headers, timeout=30,
        )
        print(f"--- event {eid} (status {resp.status_code}) ---")
        try:
            full = resp.json()
        except Exception:
            print(resp.text[:3000])
            continue
        print("top-level keys:", sorted(full.keys()))
        print("name:", full.get("name"), "| type:", full.get("type"), "| org:", full.get("organization"), "| public:", full.get("public"))
        meetings = full.get("meetings") or []
        print(f"meetings array: {len(meetings)} entries")
        for m in meetings:
            print(json.dumps(m, indent=2, default=str))
        print()

    print("=" * 70)
    print("3. /meetings pull for Oct 1-13 2026, filtered to anything Gala/Peace-related")
    print("=" * 70)
    resp = requests.get(
        f"{BASE}/api/v1/em/{SCHOOL}/meetings",
        params={"startDate": "2026-10-01", "endDate": "2026-10-13"},
        headers=headers, timeout=30,
    )
    print("status:", resp.status_code)
    try:
        data = resp.json()
    except Exception:
        print(resp.text[:3000])
        data = None

    if isinstance(data, dict) and "data" in data:
        rows = data["data"]
    elif isinstance(data, dict):
        rows = list(data.values())
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    print(f"total rows in window: {len(rows)}")
    for m in rows:
        ev = m.get("eventData") or {}
        name = (ev.get("name") or "").lower()
        if "gala" in name or "peace" in name or (m.get("eventId") in event_ids):
            print(json.dumps(m, indent=2, default=str))
            print("---")


if __name__ == "__main__":
    sys.exit(main())
