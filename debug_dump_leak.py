"""TEMPORARY debug tool -- dump raw Coursedog data for "Picnic at the Bowl"
and "Emerging Leaders Program" so we can see why they're leaking through as
"public event" despite being genuinely internal (Residential Life class
mixer, Student Affairs leadership program). Delete this file and its
workflow once the investigation is done.

v2: the multi-word /events/search/{query} call 404'd (route doesn't like
spaces the way we assumed -- unlike the earlier Peace Gala lookup, which
apparently got lucky or behaves differently). Falling back to a wider
/meetings date-range pull with substring filtering, same technique used
for the Student Staff Retreat investigation, plus trying single-word
search terms as a secondary check.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import requests

BASE = os.environ.get("COURSEDOG_READONLY_BASE", "https://app.coursedog.com")
SCHOOL = os.environ.get("COURSEDOG_SCHOOL", "soka_peoplesoft_direct")
EMAIL = os.environ["COURSEDOG_READONLY_EMAIL"]
PASSWORD = os.environ["COURSEDOG_READONLY_PASSWORD"]

SINGLE_WORD_TERMS = ["Picnic", "Emerging"]
KEYWORDS = ["picnic", "bowl", "emerging leaders"]


def get_token():
    resp = requests.post(
        f"{BASE}/api/v1/sessions",
        json={"email": EMAIL, "password": PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def dump_full_event(headers, eid):
    resp = requests.get(f"{BASE}/api/v1/em/{SCHOOL}/events/{eid}", headers=headers, timeout=30)
    print(f"--- full /events/{eid} (status {resp.status_code}) ---")
    try:
        full = resp.json()
    except Exception:
        print(resp.text[:2000])
        return
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


def main():
    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}

    print("=" * 70)
    print("Part 1: single-word /events/search attempts")
    print("=" * 70)
    seen_ids = set()
    for term in SINGLE_WORD_TERMS:
        resp = requests.get(f"{BASE}/api/v1/em/{SCHOOL}/events/search/{term}", headers=headers, timeout=30)
        print(f"search {term!r} -> status {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            print(resp.text[:500])
            continue
        items = data["data"] if isinstance(data, dict) and "data" in data and isinstance(data["data"], list) else (list(data.values()) if isinstance(data, dict) else data if isinstance(data, list) else [])
        for item in items:
            eid = item.get("_id") or item.get("id")
            print(f"  - id={eid} name={item.get('name')!r} type={item.get('type')} org={item.get('organization')}")
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                dump_full_event(headers, eid)

    print()
    print("=" * 70)
    print("Part 2: /meetings date-range pull, filtered by keyword")
    print("=" * 70)
    today = datetime.utcnow().date()
    start = today - timedelta(days=1)
    end = today + timedelta(days=3)
    resp = requests.get(
        f"{BASE}/api/v1/em/{SCHOOL}/meetings",
        params={"startDate": start.isoformat(), "endDate": end.isoformat()},
        headers=headers, timeout=30,
    )
    print(f"meetings {start} to {end} -> status {resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        print(resp.text[:2000])
        return
    rows = data["data"] if isinstance(data, dict) and "data" in data else (list(data.values()) if isinstance(data, dict) else data)
    print(f"total rows: {len(rows)}")
    for m in rows:
        blob = json.dumps(m).lower()
        if any(k in blob for k in KEYWORDS):
            eid = m.get("eventId")
            ev = m.get("eventData") or {}
            print(f"MATCH meeting {m.get('_id')} eventId={eid} name={ev.get('name')!r} type={ev.get('type')} public={ev.get('public')} start={m.get('startDate')} {m.get('startTime')}-{m.get('endTime')} isSetup={m.get('isSetup')} isTeardown={m.get('isTeardown')}")
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                dump_full_event(headers, eid)


if __name__ == "__main__":
    sys.exit(main())
