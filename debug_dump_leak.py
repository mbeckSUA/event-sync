"""TEMPORARY debug tool -- dump raw Coursedog data for "Picnic at the Bowl"
and "Emerging Leaders Program" so we can see why they're leaking through as
"public event" despite being genuinely internal (Residential Life class
mixer, Student Affairs leadership program). Delete this file and its
workflow once the investigation is done (same pattern as
debug_dump_peace_gala.py / debug_dump_retreat.py before it).
"""
import json
import os
import sys

import requests

BASE = os.environ.get("COURSEDOG_READONLY_BASE", "https://app.coursedog.com")
SCHOOL = os.environ.get("COURSEDOG_SCHOOL", "soka_peoplesoft_direct")
EMAIL = os.environ["COURSEDOG_READONLY_EMAIL"]
PASSWORD = os.environ["COURSEDOG_READONLY_PASSWORD"]

SEARCH_TERMS = ["Picnic at the Bowl", "Emerging Leaders"]


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

    for term in SEARCH_TERMS:
        print("=" * 70)
        print(f"Searching events by name for {term!r}")
        print("=" * 70)
        resp = requests.get(
            f"{BASE}/api/v1/em/{SCHOOL}/events/search/{term}",
            headers=headers, timeout=30,
        )
        print("status:", resp.status_code)
        try:
            data = resp.json()
        except Exception:
            print(resp.text[:3000])
            continue

        if isinstance(data, dict):
            items = data["data"] if "data" in data and isinstance(data["data"], list) else list(data.values())
        elif isinstance(data, list):
            items = data
        else:
            items = []

        event_ids = []
        for item in items:
            eid = item.get("_id") or item.get("id")
            print(f"  - id={eid}  name={item.get('name')}  type={item.get('type')}  org={item.get('organization')}  public={item.get('public')}")
            if eid:
                event_ids.append(eid)

        for eid in event_ids:
            resp = requests.get(
                f"{BASE}/api/v1/em/{SCHOOL}/events/{eid}",
                headers=headers, timeout=30,
            )
            print(f"--- full /events/{eid} (status {resp.status_code}) ---")
            try:
                full = resp.json()
            except Exception:
                print(resp.text[:3000])
                continue
            keep = {k: full.get(k) for k in (
                "name", "type", "organization", "public", "private",
                "status", "expectedHeadCount",
            )}
            print(json.dumps(keep, indent=2, default=str))
            meetings = full.get("meetings") or []
            print(f"meetings: {len(meetings)} entries")
            for m in meetings:
                mkeep = {k: m.get(k) for k in (
                    "startDate", "endDate", "startTime", "endTime",
                    "isSetup", "isTeardown", "status", "public", "type",
                )}
                print("  ", json.dumps(mkeep, default=str))
        print()


if __name__ == "__main__":
    sys.exit(main())
