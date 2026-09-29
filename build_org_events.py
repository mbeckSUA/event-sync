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

Each feed matches on EITHER organization OR event type — whichever field is
actually reliable for that department. Organization was the lever
pac-event-categorization.md settled on for Performing Arts Center (type is
inconsistently applied there; organization is reliable). Matches on the
live /organizations displayName each run, NOT a hardcoded org ID — the PAC
org-ID incident (a stale ID pasted into project notes, silently orphaning
events for weeks) is exactly the failure mode this avoids.

Recreation is the opposite case, discovered Sept 28: its "Recreation
Calendar" event type has an entry form that doesn't expose Organization OR
Public at all — saving an event under that type silently clears both
fields (confirmed live: reclassifying "Fitness Classes: Tennis" from Campus
Events to Recreation Calendar dropped it from this feed entirely, along
with everything else already on that type). Organization-based matching
can never work for events on that type, so Recreation matches on event
TYPE instead (match_type: "Recreation Calendar") — type is always set
(it's what selects the entry form in the first place), so it's the one
reliable signal left.

Consequence: because Public isn't on that form either, there is no way to
mark a Recreation Calendar event non-public — so require_public is False
for that feed, and EVERYTHING typed Recreation Calendar is treated as
public-facing. That's a real assumption, not a technicality: confirm with
Recreation/Susan that this type is only ever used for public programming
(intramurals, fitness classes) before trusting it, since anything entered
there for internal-only scheduling would now leak onto the public widget.

Policy note — read before flipping any require_public to False:
events-public-visibility.md flags an open, unresolved question specifically
about Recreation/student-life content: staff worry the public will assume a
"members-only" class or intramural game is open to them. Library still
defaults to public-events-only pending that same conversation.
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
# Each feed sets EITHER match_names (org-based) OR match_type (type-based),
# not both — pick whichever field that department's Coursedog data actually
# carries reliably (see the module docstring for why Recreation had to
# switch).
#
# match_names: matched case-insensitively against organization displayName.
# Exact match tried first; falls back to substring match with a logged
# warning if nothing matches exactly (so a rename in Coursedog degrades
# loudly instead of silently going empty). List multiple names if a
# department's Coursedog org is split across sub-orgs.
#
# match_type: matched exactly against event type. Use this when the
# department's event type doesn't carry Organization/Public (Recreation
# Calendar, confirmed Sept 28) — see the module docstring.
#
# require_public: only include events explicitly marked public. Must be
# False for a match_type feed whose type has no Public field at all — see
# the policy note in the module docstring before changing this for any
# other feed.
# ---------------------------------------------------------------------------
FEEDS = [
    {
        "key": "recreation",
        "match_type": "Recreation Calendar",
        "require_public": False,
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


def fetch_events_dict(token):
    """Bulk /events listing -- same {"data": [...], "totalCount": N} shape as
    /meetings -- used as the authoritative source for fields that /meetings'
    nested eventData sometimes omits entirely (confirmed Sept 29: eventData
    was missing "description" -- not blank, the key was absent -- for an
    event where the direct /events/{id} record had the full text; eventData
    also had fewer customFields than the direct record). Rather than guess
    why /meetings under-projects a given event, this pulls the full record
    once per run and lets transform() prefer it when present."""
    headers = {"Authorization": f"Bearer {token}"}
    events, skip = {}, 0
    while True:
        resp = requests.get(f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/events",
            params={"skip": skip, "limit": PAGE_LIMIT}, headers=headers, timeout=30)
        resp.raise_for_status()
        page = resp.json()
        batch = page.get("data", []) if isinstance(page, dict) else (page or [])
        if not batch:
            break
        for e in batch:
            key = e.get("_id") or e.get("id")
            if key:
                events[key] = e
        if len(batch) < PAGE_LIMIT:
            break
        skip += PAGE_LIMIT
    log.info(f"Pulled {len(events)} events from bulk /events listing")
    return events


def org_display(org_rec):
    """displayName with a name fallback — the org dict isn't guaranteed to
    have both keys populated the same way every time."""
    return (org_rec.get("displayName") or org_rec.get("name")) if org_rec else None


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
                        f"{[org_display(orgs[oid]) for oid in substr_hits]}")
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


def is_excluded(meeting, feed, org_ids):
    ev = meeting.get("eventData") or {}
    if ev.get("private") is True:
        return True
    if meeting.get("isSetup") or meeting.get("isTeardown"):
        return True
    if ev.get("status") != "Confirmed":
        return True
    if ev.get("type") in EXCLUDE_TYPES:
        return True

    if feed.get("match_type"):
        if ev.get("type") != feed["match_type"]:
            return True
    else:
        if ev.get("organization") not in org_ids:
            return True

    if feed["require_public"] and not ev.get("public"):
        return True
    return False


def transform(meeting, rooms, orgs, events_by_id):
    ev = meeting.get("eventData") or {}
    # /meetings' nested eventData can be missing fields the full record has
    # (confirmed: description entirely absent, not just blank, for at least
    # one event) -- prefer the bulk /events record's description when it's
    # there, and only fall back to eventData's copy otherwise.
    full_ev = events_by_id.get(meeting.get("eventId")) or {}
    description_source = full_ev.get("description") or ev.get("description") or ""
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
    org_name = org_display(orgs.get(org_id)) if org_id else None

    return {
        "_event_id": meeting.get("eventId") or ev.get("_id"),
        "name": ev.get("name", ""),
        "type": ev.get("type", ""),
        "public": bool(ev.get("public")),
        "description": description_source.strip(),
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
    """One row per event, but — unlike build_campus_events.py's version —
    each occurrence keeps its own date/time. A single Coursedog event can
    carry multiple recurrence patterns (e.g. Mon 7-8pm AND Wed 6:30-7:30pm
    under one event id), and the old flat-occurrenceDates approach forced
    every date to show whichever time belonged to the earliest occurrence —
    wrong for any date on a different pattern (discovered via "Fitness
    Classes: Tennis", Sept 2026). occurrences[] is now the source of truth
    for what to render on which date. The top-level date/startTime/endTime/
    allDay stay as a summary (earliest occurrence) for anything that only
    wants one line, and occurrenceDates is kept for back-compat but should
    no longer be used to look up a time."""
    groups = defaultdict(list)
    for row in rows:
        key = row["_event_id"] or (row["name"], row["room"])
        groups[key].append(row)

    out = []
    for occurrences in groups.values():
        occurrences.sort(key=lambda r: (r["date"] or "", r["startTime"] or ""))
        first = dict(occurrences[0])
        first["occurrenceCount"] = len(occurrences)
        first["occurrences"] = [
            {
                "date": r["date"],
                "startTime": r["startTime"],
                "endTime": r["endTime"],
                "allDay": r["allDay"],
            }
            for r in occurrences if r["date"]
        ]
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
    events_by_id = fetch_events_dict(token)
    log.info("Live organizations: " + ", ".join(sorted(
        (o.get("displayName") or o.get("name") or "?") for o in orgs.values())))

    today = datetime.now(PACIFIC).date()
    start = today.strftime("%Y-%m-%d")
    end = (today + timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    raw = fetch_meetings(token, start, end)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    for feed in FEEDS:
        if feed.get("match_type"):
            org_ids = None
            match_desc = f"type == {feed['match_type']!r}"
        else:
            org_ids = resolve_feed_org_ids(feed, orgs)
            match_desc = f"org match: {[org_display(orgs[oid]) for oid in org_ids]}"

        kept = [m for m in raw if not is_excluded(m, feed, org_ids)]
        log.info(f"[{feed['key']}] {len(kept)} of {len(raw)} meetings kept "
                 f"({match_desc}, require_public={feed['require_public']})")

        rows = [transform(m, rooms, orgs, events_by_id) for m in kept]
        events = group_and_dedupe(rows)
        events.sort(key=lambda e: (e["date"] or "", e["startTime"] or ""))

        feed["out_file"].write_text(json.dumps(events, indent=2))
        log.info(f"[{feed['key']}] wrote {len(events)} events to {feed['out_file']}")


if __name__ == "__main__":
    main()
