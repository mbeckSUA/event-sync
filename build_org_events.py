#!/usr/bin/env python3
"""
Org-specific event feeds for SharePoint — Recreation, Library, and any other
department that wants its own filtered widget, reusing the same read-only
Coursedog pull as build_campus_events.py.

Deliberately its own script rather than a modification to
build_campus_events.py: that one is running in production and feeds the live
campus calendar; this one is new and lower-stakes, so it stays isolated
rather than risking the working pipeline. Some logic (auth, meeting-window
pagination gotcha, lookup-dict resolution, recurring-event dedup) is
duplicated from there on purpose — see api-learnings.md and the comments in
build_campus_events.py for why each piece works the way it does.

Filters by ORGANIZATION, not event type — same lever pac-event-categorization.md
settled on for Performing Arts Center (type is inconsistently applied;
organization is reliable). Matches on the live /organizations displayName
each run, NOT a hardcoded org ID — the PAC org-ID incident (a stale ID
pasted into project notes, silently orphaning events for weeks) is exactly
the failure mode this avoids. If a feed's configured name stops matching
anything, this logs a warning and writes an empty feed rather than quietly
serving stale or wrong data.

Policy note — read before flipping REQUIRE_PUBLIC to False:
events-public-visibility.md flags an open, unresolved question specifically
about Recreation/student-life content: staff worry the public will assume a
"members-only" class or intramural game is open to them. Defaulting every
feed here to public-events-only is the conservative choice until that's
actually settled with the department — don't loosen it unilaterally.
"""
import json, logging, os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

PACIFIC = ZoneInfo("America/Los_Angeles")
DOCS_DIR = Path(__file__).parent / "docs"

COURSEDOG_BASE = os.environ["COURSEDOG_READONLY_BASE"]
COURSEDOG_EMAIL = os.environ["COURSEDOG_READONLY_EMAIL"]
COURSEDOG_PASSWORD = os.environ["COURSEDOG_READONLY_PASSWORD"]
COURSEDOG_SCHOOL = os.environ["COURSEDOG_SCHOOL"]

WINDOW_DAYS = int(os.environ.get("EVENTS_WINDOW_DAYS", "365"))
PAGE_LIMIT = 200

# Same exclusions as build_campus_events.py — room bookings people need to
# confirm went through, not "happenings" for a department widget either.
EXCLUDE_TYPES = {"Internal Meeting", "Student Request Form"}

# ---------------------------------------------------------------------------
# Feed configuration. Add a new dict here for each department feed wanted —
# no new GitHub secret needed, this reuses the existing read-only credentials.
#
# match_names: matched case-insensitively against organization displayName.
# Exact match tried first; falls back to substring match with a logged
# warning if nothing matches exactly (so a rename in Coursedog degrades
# loudly instead of silently going empty). List multiple names if a
# department's Coursedog org is split across sub-orgs.
#
# require_public: only include events the organizer explicitly marked
# public. See the policy note in the module docstring before changing this.
# ---------------------------------------------------------------------------
FEEDS = [
    {
        "key": "recreation",
        "match_names": ["Recreation"],
        "require_public": True,
        "out_file": DOCS_DIR / "recreation-events.json",
    },
    {
        "key": "library",
        "match_names": ["Library"],
        "require_public": True,
        "out_file": DOCS_DIR / "library-events.json",
    },
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def get_token():
    resp = requests.post(f"{COURSEDOG_BASE}/api/v1/sessions",
        json={"email": COURSEDOG_EMAIL, "password": COURSEDOG_PASSWORD}, timeout=30)
    resp.raise_for_status()
    return resp.json()["token"]


def daterange_chunks(start_date, end_date, chunk_days=30):
    """Same chunking as build_campus_events.py — a single wide-range /meetings
    query silently truncates (confirmed live, see api-learnings.md); querying
    in month-sized windows keeps pagination honest."""
    cur = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        yield cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")
        cur = chunk_end + timedelta(days=1)


def fetch_meetings_window(token, start_date, end_date):
    headers = {"Authorization": f"Bearer {token}"}
    meetings, skip = [], 0
    while True:
        resp = requests.get(
            f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/meetings",
            params={"startDate": start_date, "endDate": end_date, "skip": skip, "limit": PAGE_LIMIT},
            headers=headers, timeout=30,
        )
        resp.raise_for_status()
        page = resp.json()
        if isinstance(page, list):
            batch = page
        elif isinstance(page, dict):
            batch = page.get("data") or page.get("meetings") or list(page.values())
        else:
            batch = []
        if not batch:
            break
        meetings.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        skip += PAGE_LIMIT
    return meetings


def fetch_meetings(token, start_date, end_date):
    seen = {}
    for chunk_start, chunk_end in daterange_chunks(start_date, end_date):
        batch = fetch_meetings_window(token, chunk_start, chunk_end)
        for m in batch:
            key = m.get("_id") or m.get("id")
            if key not in seen:
                seen[key] = m
    meetings = list(seen.values())
    log.info(f"Pulled {len(meetings)} unique meetings, {start_date}..{end_date}")
    return meetings


def fetch_lookup_dict(token, resource_candidates):
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
        log.info(f"Loaded {len(out)} records from /{resource}")
        return out
    log.warning(f"None of {resource_candidates} worked")
    return {}


def resolve_feed_org_ids(feed, orgs):
    """Matches feed['match_names'] against live org displayNames. Exact
    case-insensitive match first; substring fallback with a warning."""
    by_name = {(o.get("displayName") or o.get("name") or "").strip().lower(): oid
               for oid, o in orgs.items()}
    matched = set()
    for wanted in feed["match_names"]:
        wanted_l = wanted.strip().lower()
        if wanted_l in by_name:
            matched.add(by_name[wanted_l])
            continue
        substr_hits = [oid for name, oid in by_name.items() if wanted_l in name]
        if substr_hits:
            log.warning(f"[{feed['key']}] no exact org match for '{wanted}' — "
                        f"using {len(substr_hits)} substring match(es) instead: "
                        f"{[orgs[oid].get('displayName') for oid in substr_hits]}")
            matched.update(substr_hits)
        else:
            log.warning(f"[{feed['key']}] '{wanted}' matched NO organization — "
                        f"check the live org list below; feed will be empty")
    return matched


def fmt_time(hhmm):
    if hhmm in (None, ""):
        return None
    hhmm = int(hhmm)
    h, m = divmod(hhmm, 100)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def is_excluded(meeting, org_ids, require_public):
    ev = meeting.get("eventData") or {}
    if ev.get("private") is True:
        return True
    if meeting.get("isSetup") or meeting.get("isTeardown"):
        return True
    if ev.get("status") != "Confirmed":
        return True
    if ev.get("type") in EXCLUDE_TYPES:
        return True
    if ev.get("organization") not in org_ids:
        return True
    if require_public and not ev.get("public"):
        return True
    return False


def transform(meeting, rooms, orgs):
    ev = meeting.get("eventData") or {}
    contacts = ev.get("contacts") or []
    contact = None
    if contacts:
        primary = contacts[0]
        contact = {"name": primary.get("name") or None, "email": primary.get("email") or None}

    room_id = meeting.get("roomId")
    room_rec = rooms.get(room_id) if room_id else None
    room_name = (room_rec.get("displayName") or room_rec.get("name")) if room_rec else None
    building_name = room_rec.get("buildingDisplayName") if room_rec else None

    org_id = ev.get("organization")
    org_rec = orgs.get(org_id) if org_id else None
    org_name = (org_rec.get("displayName") or org_rec.get("name")) if org_rec else None

    return {
        "_event_id": meeting.get("eventId") or ev.get("_id"),
        "name": ev.get("name", ""),
        "type": ev.get("type", ""),
        "public": bool(ev.get("public")),
        "description": (ev.get("description") or "").strip(),
        "extendedDescription": ev.get("extendedDescription") or "",
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
    """Same recurring-event collapse as build_campus_events.py — one row per
    event, anchored at its earliest date, with every occurrence date kept so
    a multi-month series doesn't vanish from the widget after its first
    showing."""
    groups = defaultdict(list)
    for row in rows:
        key = row["_event_id"] or (row["name"], row["room"])
        groups[key].append(row)

    out = []
    for occurrences in groups.values():
        occurrences.sort(key=lambda r: (r["date"] or "", r["startTime"] or ""))
        first = dict(occurrences[0])
        first["occurrenceCount"] = len(occurrences)
        dates = sorted({r["date"] for r in occurrences if r["date"]})
        first["occurrenceDates"] = dates
        first["lastDate"] = dates[-1] if dates else first["date"]
        del first["_event_id"]
        out.append(first)
    return out


def main():
    token = get_token()
    log.info("Authenticated with Coursedog (read-only user)")

    rooms = fetch_lookup_dict(token, ["rooms"])
    orgs = fetch_lookup_dict(token, ["organizations", "orgs", "departments"])
    log.info("Live organizations: " + ", ".join(sorted(
        (o.get("displayName") or o.get("name") or "?") for o in orgs.values())))

    today = datetime.now(PACIFIC).date()
    start = today.strftime("%Y-%m-%d")
    end = (today + timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    raw = fetch_meetings(token, start, end)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    for feed in FEEDS:
        org_ids = resolve_feed_org_ids(feed, orgs)
        kept = [m for m in raw if not is_excluded(m, org_ids, feed["require_public"])]
        log.info(f"[{feed['key']}] {len(kept)} of {len(raw)} meetings kept "
                 f"(org match: {[orgs[oid].get('displayName') for oid in org_ids]}, "
                 f"require_public={feed['require_public']})")

        rows = [transform(m, rooms, orgs) for m in kept]
        events = group_and_dedupe(rows)
        events.sort(key=lambda e: (e["date"] or "", e["startTime"] or ""))

        feed["out_file"].write_text(json.dumps(events, indent=2))
        log.info(f"[{feed['key']}] wrote {len(events)} events to {feed['out_file']}")


if __name__ == "__main__":
    main()
