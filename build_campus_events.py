#!/usr/bin/env python3
"""
Pulls upcoming Coursedog meetings and writes docs/events.json — the data
file campus_happenings.html and campus_happenings_sidebar.html fetch at
load time, replacing the baked-in static EVENTS array.

Read-only: uses the dedicated read-only API user (see api-user-setup.md),
never writes to Coursedog.

Confirmed against a real production pull (Sept 11): meeting.eventId ==
eventData._id (safe to group recurring meetings on), eventData.contacts
is a list, and neither room/building name nor organization name live on
the meeting — both need a lookup against /rooms and /organizations
(tried in that order, with /orgs and /departments as fallback names
since the real one isn't confirmed yet).

Still open: whether /organizations (or a fallback) actually resolves —
check the "Loaded N records from /..." log line, and if none of the
candidates worked, org will come back blank rather than a raw UUID.
"""
import json, logging, os, sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

PACIFIC = ZoneInfo("America/Los_Angeles")
OUT_FILE = Path(__file__).parent / "docs" / "events.json"
DEBUG_SAMPLE_FILE = Path(__file__).parent / "debug_sample.json"
RAW_PAGE_FILE = Path(__file__).parent / "debug_raw_page.json"

COURSEDOG_BASE = os.environ["COURSEDOG_READONLY_BASE"]
COURSEDOG_EMAIL = os.environ["COURSEDOG_READONLY_EMAIL"]
COURSEDOG_PASSWORD = os.environ["COURSEDOG_READONLY_PASSWORD"]
COURSEDOG_SCHOOL = os.environ["COURSEDOG_SCHOOL"]

WINDOW_DAYS = int(os.environ.get("EVENTS_WINDOW_DAYS", "90"))
PAGE_LIMIT = 200

# Provisional per events-public-visibility.md (Sept 9 check) — confirmed
# against 5 real records, not exhaustive. Revisit if a new eventData.type
# value shows up that should also be excluded.
EXCLUDE_TYPES = {"Academic Calendar"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def get_token():
    resp = requests.post(f"{COURSEDOG_BASE}/api/v1/sessions",
        json={"email": COURSEDOG_EMAIL, "password": COURSEDOG_PASSWORD}, timeout=30)
    resp.raise_for_status()
    return resp.json()["token"]


def fetch_meetings(token, start_date, end_date):
    """Paginated pull of every meeting in the window. Returns raw meeting dicts."""
    headers = {"Authorization": f"Bearer {token}"}
    meetings = []
    skip = 0
    dumped_raw = False
    while True:
        resp = requests.get(
            f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/meetings",
            params={"startDate": start_date, "endDate": end_date, "skip": skip, "limit": PAGE_LIMIT},
            headers=headers, timeout=30,
        )
        resp.raise_for_status()
        page = resp.json()

        if not dumped_raw:
            # Always capture the very first raw response, whatever shape it
            # turns out to be, BEFORE trying to interpret it. Guessing wrong
            # here is exactly what happened on the first live run (0 meetings
            # came back even though real data exists in this window) — this
            # is how we find out what actually came back instead of guessing
            # a second time.
            RAW_PAGE_FILE.write_text(json.dumps(page, indent=2, default=str)[:500000])
            log.info(f"Response type: {type(page).__name__}"
                      + (f", top-level keys: {list(page.keys())}" if isinstance(page, dict) else "")
                      + f" — wrote raw first page to {RAW_PAGE_FILE}")
            dumped_raw = True

        # Response shape not yet confirmed at scale. /events shifted from a
        # bare dict/list to {data: [...], totalCount} on Aug 12; /meetings may
        # or may not have followed. Rooms/orgs endpoints instead return a
        # dict keyed by internal id (see api-learnings.md) — try that too.
        if isinstance(page, list):
            batch = page
        elif isinstance(page, dict):
            if "data" in page:
                batch = page["data"]
            elif "meetings" in page:
                batch = page["meetings"]
            else:
                batch = list(page.values())
        else:
            batch = []

        if not batch:
            break
        meetings.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        skip += PAGE_LIMIT
    log.info(f"Pulled {len(meetings)} raw meetings, {start_date}..{end_date}")
    return meetings


def is_excluded(meeting):
    ev = meeting.get("eventData") or {}
    # Belt-and-suspenders per events-public-visibility.md — Coursedog already
    # redacts private:true server-side, but skip explicitly too.
    if ev.get("private") is True:
        return True
    if meeting.get("isSetup") or meeting.get("isTeardown"):
        return True
    if ev.get("type") in EXCLUDE_TYPES:
        return True
    return False


def fmt_time(hhmm):
    """Coursedog meeting times come back as an int/str like 1900 (7:00 PM),
    or absent for all-day meetings."""
    if hhmm in (None, ""):
        return None
    hhmm = int(hhmm)
    h, m = divmod(hhmm, 100)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def fetch_lookup_dict(token, resource_candidates):
    """Rooms/organizations endpoints return a dict keyed by internal id, same
    as /meetings turned out to (see api-learnings.md). Tries each candidate
    resource name in order and uses the first that responds; logs which one
    worked (or that none did) rather than assuming."""
    headers = {"Authorization": f"Bearer {token}"}
    for resource in resource_candidates:
        try:
            resp = requests.get(f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/{resource}",
                headers=headers, timeout=30)
            resp.raise_for_status()
        except requests.HTTPError as e:
            log.info(f"/{resource} not usable ({e.response.status_code if e.response is not None else e}), trying next candidate")
            continue
        data = resp.json()
        if isinstance(data, dict):
            out = data
        elif isinstance(data, list):
            out = {item.get("id") or item.get("_id"): item for item in data}
        else:
            out = {}
        log.info(f"Loaded {len(out)} records from /{resource} for lookup")
        return out
    log.warning(f"None of {resource_candidates} worked — falling back to blank instead of a raw id in the display")
    return {}


def transform(meeting, rooms, orgs):
    ev = meeting.get("eventData") or {}

    # Confirmed live (Sept 11 run): eventData.contacts is a LIST, not a
    # single "contact" field. Widget shows one contact — use the first.
    contacts = ev.get("contacts") or []
    contact = None
    if contacts:
        primary = contacts[0]
        contact = {"name": primary.get("name") or None, "email": primary.get("email") or None}

    # Confirmed live: no room/building name lives on the meeting itself —
    # only a bare roomId (sometimes a short SIS code, sometimes a UUID, per
    # §3.3 of the main guide). Resolve it against the /rooms lookup; if that
    # lookup failed or doesn't have this id, leave it blank rather than show
    # a raw code in the widget.
    room_id = meeting.get("roomId")
    room_rec = rooms.get(room_id) if room_id else None
    room_name = (room_rec.get("displayName") or room_rec.get("name")) if room_rec else None
    building_name = (room_rec.get("building") or room_rec.get("buildingName")) if room_rec else None

    # Same problem, same fix, for organization — eventData.organization is a
    # bare UUID with no readable name attached to the meeting.
    org_id = ev.get("organization")
    org_rec = orgs.get(org_id) if org_id else None
    org_name = (org_rec.get("displayName") or org_rec.get("name")) if org_rec else None

    return {
        # Confirmed live: meeting.eventId matches eventData._id exactly —
        # this is the right key to collapse recurring meetings into one row.
        "_event_id": meeting.get("eventId") or ev.get("_id"),
        "name": ev.get("name", ""),
        "type": ev.get("type", ""),
        "public": bool(ev.get("public")),
        "description": (ev.get("description") or "").strip(),
        "extendedDescription": ev.get("extendedDescription") or "",
        "facility": bool(ev.get("facility")),
        "date": meeting.get("startDate"),
        "startTime": fmt_time(meeting.get("startTime")),
        "endTime": fmt_time(meeting.get("endTime")),
        "allDay": bool(meeting.get("allDay")),
        "room": room_name,
        "building": building_name,
        "imageURL": ev.get("imageURL", ""),
        "contact": contact,
        "org": org_name,
    }


def group_and_dedupe(rows):
    """One row per distinct event, at its earliest date in the window, with
    occurrenceCount = how many meetings that event has in the window. Matches
    the widget's "multi-day/recurring items show their first date; xN tag"
    behavior."""
    groups = defaultdict(list)
    for row in rows:
        key = row["_event_id"] or (row["name"], row["room"])
        groups[key].append(row)

    out = []
    for occurrences in groups.values():
        occurrences.sort(key=lambda r: (r["date"] or "", r["startTime"] or ""))
        first = dict(occurrences[0])
        first["occurrenceCount"] = len(occurrences)
        del first["_event_id"]
        out.append(first)
    return out


def main():
    token = get_token()
    log.info("Authenticated with Coursedog (read-only user)")

    rooms = fetch_lookup_dict(token, ["rooms"])
    orgs = fetch_lookup_dict(token, ["organizations", "orgs", "departments"])

    today = datetime.now(PACIFIC).date()
    start = today.strftime("%Y-%m-%d")
    end = (today + timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")

    raw = fetch_meetings(token, start, end)
    if raw:
        DEBUG_SAMPLE_FILE.write_text(json.dumps(raw[:5], indent=2))
        log.info(f"Wrote a 5-record raw sample to {DEBUG_SAMPLE_FILE} — read it before trusting events.json")
    else:
        log.warning("No meetings returned — check credentials/date window before assuming this is correct")

    kept = [m for m in raw if not is_excluded(m)]
    log.info(f"{len(kept)} of {len(raw)} meetings kept after filtering (private / setup / teardown / {sorted(EXCLUDE_TYPES)})")

    rows = [transform(m, rooms, orgs) for m in kept]
    events = group_and_dedupe(rows)
    events.sort(key=lambda e: (e["date"] or "", e["startTime"] or ""))

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(events, indent=2))
    log.info(f"Wrote {len(events)} events to {OUT_FILE}")


if __name__ == "__main__":
    main()
