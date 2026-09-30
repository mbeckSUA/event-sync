"""TEMPORARY debug tool -- before flipping is_public_facing()'s default
(require public:true for every type except a short exempt list, instead of
"public unless excluded"), check whether known-important events that
currently surface correctly would SURVIVE that flip. Specifically:
"EBP End of Program Ceremony" and "SBP/SWC Completion Ceremony" (both
confirmed entered as type "Campus Events", both genuine outside-visitor
rental-shaped events per the 2026-09-29 investigation) -- do they actually
have public:true, or were they only surfacing because Campus Events
defaults to public? If the latter, flipping the default would silently
un-flag them, which is a worse failure than any over-flagging we're
fixing. Delete this file and its workflow once the investigation is done.
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

# Wide window -- these are named/dated events we don't have exact dates
# for, so scan broadly rather than guess.
KEYWORDS = ["ebp", "swc", "completion ceremony", "end of program",
            "end-of-program"]


def get_token():
    resp = requests.post(f"{BASE}/api/v1/sessions", json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    resp.raise_for_status()
    return resp.json()["token"]


def main():
    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}
    seen_ids = set()

    today = datetime.utcnow().date()
    start = today - timedelta(days=400)
    end = today + timedelta(days=30)
    print(f"Scanning meetings {start} to {end} for: {KEYWORDS}")

    # Coursedog's /meetings endpoint appears to want reasonably sized
    # windows; chunk by 30 days to be safe rather than one huge range.
    cur = start
    total = 0
    while cur < end:
        chunk_end = min(cur + timedelta(days=30), end)
        resp = requests.get(
            f"{BASE}/api/v1/em/{SCHOOL}/meetings",
            params={"startDate": cur.isoformat(), "endDate": chunk_end.isoformat()},
            headers=headers, timeout=30,
        )
        if resp.status_code != 200:
            print(f"  {cur} to {chunk_end}: status {resp.status_code}, skipping")
            cur = chunk_end
            continue
        data = resp.json()
        rows = data["data"] if isinstance(data, dict) and "data" in data else (list(data.values()) if isinstance(data, dict) else data)
        total += len(rows)
        for m in rows:
            blob = json.dumps(m).lower()
            if any(k in blob for k in KEYWORDS):
                eid = m.get("eventId")
                ev = m.get("eventData") or {}
                print(f"MATCH eventId={eid} name={ev.get('name')!r} type={ev.get('type')} public={ev.get('public')} org={ev.get('organization')} start={m.get('startDate')}")
                if eid and eid not in seen_ids:
                    seen_ids.add(eid)
        cur = chunk_end
    print(f"Scanned {total} total meeting rows across the window.")

    print()
    print("=== Full event records for matches ===")
    for eid in seen_ids:
        resp = requests.get(f"{BASE}/api/v1/em/{SCHOOL}/events/{eid}", headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"{eid}: status {resp.status_code}")
            continue
        full = resp.json()
        keep = {k: full.get(k) for k in ("name", "type", "organization", "public", "private", "status", "expectedHeadCount")}
        print(json.dumps(keep, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
