#!/usr/bin/env python3
"""
Daily campus-security event report for Soka University.

Two ways to run this:

1. Against a Coursedog export CSV (manual mode):
       python security_report.py --csv path/to/export.csv --date 2026-09-25

2. Live against the Coursedog API (automated mode — what the scheduled
   GitHub Action uses):
       python security_report.py --live --date 2026-09-25 --days 6

--days (live mode only, default 1) renders that many days as tabs on the
published page, starting at --date — a bounded lookahead window (e.g.
today + next 5), not open-ended date browsing. See project notes for why
it's capped rather than unlimited: a wider window is trivial from a data-
load standpoint, but it multiplies exposure on an unauthenticated URL and
near-term future days are only as fresh as the run that generated them.
--send and stdout always cover just --date, regardless of --days.

Delivery, either mode:
    --publish PATH   write a standalone HTML page to PATH (the primary
                      delivery method — see docs/<slug>/index.html, published
                      via GitHub Pages, same pattern as the campus-happenings
                      viewer). Creates parent directories as needed.
    --send           email the report, if SMTP env vars are set. Not the
                      current delivery method (SUA's O365 tenant makes plain
                      SMTP auth a headache — see project notes) but left in
                      place in case that changes.

Always prints a plain-text version to stdout regardless of the above.

Env vars used in --live mode:
    COURSEDOG_READONLY_BASE      default: https://app.coursedog.com
    COURSEDOG_SCHOOL             default: soka_peoplesoft_direct
    COURSEDOG_READONLY_EMAIL
    COURSEDOG_READONLY_PASSWORD
    (dedicated read-only API user — same convention as build_campus_events.py.
    This script only ever does GET requests; never point it at a read-write
    credential.)

Env vars used for email (either mode, only if --send is passed):
    SMTP_HOST
    SMTP_PORT            default: 587
    SMTP_USER
    SMTP_PASSWORD
    SECURITY_EMAIL_FROM
    SECURITY_EMAIL_TO    comma-separated list
"""

import argparse
import csv
import os
import smtplib
import sys
from collections import defaultdict
from datetime import datetime, date, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------------------
# Flagging rules — what counts as "notable" for a campus security reader.
# Tuned from Soka's real event-name/org conventions (see project notes);
# adjust freely as the team learns what's actually useful.
# ---------------------------------------------------------------------------

AFTER_HOURS_START = 7 * 60   # 7:00 AM, in minutes since midnight
AFTER_HOURS_END = 21 * 60    # 9:00 PM

SETUP_TEARDOWN_PREFIXES = ("setup:", "teardown:")

# Attendance size dropped as a flagging criterion — per Martin, PAC shows
# already draw crowds and everyone knows it; that's not new information.
# The thing worth surfacing is who's on campus, not how many. Two tiers:
# outside visitors (top), everything else (secondary).

# Per the project's PAC-categorization notes: real ticketed PAC shows are
# identified by organization == "Soka Performing Arts Center" AND public
# == true, NOT by the event `type` field (which is unreliable for PAC's
# own shows). CSV exports don't carry a `public` column, so in that mode
# we approximate "public" as "not an internal setup/teardown/logistics
# entry" — good enough to catch the show itself even if it under- or
# over-flags PAC's internal prep work.
PAC_ORG_NAME = "soka performing arts center"

# Confirmed real eventData.type value (see build_campus_events.py's
# EXTERNAL_UNLESS_PUBLIC_TYPES) — reliable, not a guess. Only present in
# --live mode; CSV exports don't carry a type column.
RENTAL_TYPE = "External Rental"

# Confirmed Sept 30: "Shishiza: Leverages On-Campus Event" came through as
# type "Student Organization Event" -- not in INTERNAL_ONLY_TYPES or
# CAMPUS_COMMUNITY_TYPES, so it was about to be flagged "public event" by
# default. Per Martin: treat student org events as private unless someone
# specifically marks them for the public calendar (same "unless marked
# public" shape as External Rental above) -- most club activity isn't
# meant to draw outside visitors, and the ones that are can still surface
# themselves by checking `public`.
STUDENT_ORG_TYPE = "Student Organization Event"

# Types that are treated as public-facing ONLY when `public` is explicitly
# true; otherwise assumed private/internal. See RENTAL_TYPE and
# STUDENT_ORG_TYPE comments above for the reasoning behind each.
PUBLIC_UNLESS_MARKED_TYPES = {RENTAL_TYPE, STUDENT_ORG_TYPE}

# Admissions hosts group tours/visits for prospective students and their
# families -- Coursedog has no dedicated event type for this (confirmed
# Sept 29: "HS Group Tour - Camino Nuevo Charter Academy..." came through
# as type "Internal Meeting", public: false -- same as a genuinely internal
# staff meeting). Since neither `type` nor `public` distinguishes a tour
# from real internal business for this org, match on name instead: weaker
# than a dedicated field, but Admissions' tour-booking names are
# consistently prefixed this way in practice. Under-flagging (a
# differently-named tour slips through) is the likely failure mode, not
# over-flagging -- revisit the name hints if that turns out to be common.
ADMISSIONS_ORG_NAME = "admissions"
VISIT_NAME_HINTS = ("group tour", "campus tour", "open house", "shadow day",
                     "prospective", "discover soka", "experience soka")

# Same set as build_campus_events.py's EXCLUDE_TYPES. Confirmed Sept 29:
# "Student Staff Retreat" (type "Internal Meeting", genuinely internal --
# same-day campus feed pull has it as public: false) still showed up here
# flagged "public event" three times, because its `public` field came back
# true from this script's own independent /meetings pull. This is the same
# denormalization unreliability already confirmed for `description` earlier
# the same day (see recreation-library-feeds.md in the Coursedog API
# project) -- /meetings' per-occurrence eventData snapshot can disagree
# with itself run to run. `type` is a deliberate, stable field nobody sets
# by accident, so it overrides a possibly-stale `public` flag rather than
# the other way around.
INTERNAL_ONLY_TYPES = {"Internal Meeting", "Student Request Form"}

# Confirmed live 2026-09-29 (first published run after the events.soka.edu-
# based public-event rework): "Fitness Classes: Strength and Conditioning",
# "Intramurals: Badminton", "Intramurals: Volleyball", etc. were showing up
# in the Outside Visitors tier as "public event." These are Coursedog type
# "Recreation Calendar" -- they do reach events.soka.edu (build_org_events.py
# publishes a whole Recreation department feed from them), but that's a
# campus-community audience, not the general public: nobody's driving up to
# the guard shack for intramural badminton. is_public_facing() mirrors "does
# this reach the public calendar," which was the wrong proxy here -- the
# actual test (per Martin) is "will this put a non-SUA person at the gate,"
# and department-internal recreation programming doesn't. Same open question
# already on file in recreation-library-feeds.md (whether Recreation/Library
# should require public:true for their own feed); this is the security-
# report-specific answer to that question: no, for this purpose, treat the
# whole type as campus-community regardless of `public`. Revisit if
# Recreation ever runs something genuinely open to outside registrants (a
# public 5k, a community class) -- that would need its own signal, not a
# blanket type exclusion.
CAMPUS_COMMUNITY_TYPES = {"Recreation Calendar"}

# Fallback for CSV mode, where there's no `type` field to check: org name
# is a weaker signal (Events & Conferences also runs some internal-facing
# bookings), so this gets its own distinct, lower-confidence flag rather
# than being treated the same as a confirmed External Rental type match.
RENTAL_ORG_HINT = "events & conferences"

CANCELED_STATUSES = {"canceled", "cancelled", "denied"}

# Tiers, in the order they appear in the report.
OUTSIDE_VISITOR_FLAGS = {"rental", "rental (unconfirmed)", "public event", "campus visit"}


def parse_time_to_minutes(raw):
    """Coursedog export times look like '9:00 AM' or the placeholder
    "'--:--:--" for events with no specific time (all-day / TBD)."""
    if not raw or "--" in raw:
        return None
    raw = raw.strip().lstrip("'")
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            t = datetime.strptime(raw, fmt)
            return t.hour * 60 + t.minute
        except ValueError:
            continue
    return None


def format_minutes(m):
    if m is None:
        return "TBD"
    h, mm = divmod(m, 60)
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{mm:02d} {ampm}"


def is_public_facing(event):
    """Would this event actually display on the public campus calendar
    (events.soka.edu)? Ported from build_campus_events.py's is_excluded()
    (inverted) so this report's "public event" flag means what Martin
    needs it to mean: not "Coursedog's `public` checkbox happens to be
    ticked" but "a member of the public can find this on the calendar and
    show up." Confirmed 2026-09-29 by reading that script's actual
    criteria: almost everything reaches the public calendar regardless of
    the `public` field -- it's excluded only for a specific, narrow set of
    reasons (private, setup/teardown, not Confirmed, an internal-only
    type, or an External Rental nobody marked public). Using `public:
    true` alone, as this report did before, was both over-inclusive (see
    the Student Staff Retreat false positive elsewhere in this file) and
    under-inclusive (it missed real public-facing events that never had
    `public` ticked, which is apparently most of them).

    Only meaningful in live mode, where type/status/private are populated
    from real Coursedog data -- see the call site in classify()."""
    if event.get("is_redacted") or event.get("private"):
        return False
    if event.get("is_setup") or event.get("is_teardown"):
        return False
    if (event.get("status") or "").strip().lower() != "confirmed":
        return False
    event_type = event.get("type")
    if event_type in INTERNAL_ONLY_TYPES or event_type in CAMPUS_COMMUNITY_TYPES:
        return False
    if event_type in PUBLIC_UNLESS_MARKED_TYPES and not event.get("public"):
        return False
    return True


def classify(event):
    """Return a list of flag strings for a single event/meeting row."""
    flags = []
    name_lower = event["name"].lower()
    status_lower = event["status"].lower()

    if status_lower in CANCELED_STATUSES:
        flags.append("canceled")
        return flags  # don't bother with other flags on a canceled event

    is_setup_or_teardown = (
        bool(event.get("is_setup")) or bool(event.get("is_teardown"))
        or any(name_lower.startswith(p) for p in SETUP_TEARDOWN_PREFIXES)
    )
    if is_setup_or_teardown:
        flags.append("setup/teardown")

    start_m = event["start_min"]
    end_m = event["end_min"]
    if start_m is not None and start_m < AFTER_HOURS_START:
        flags.append("early morning")
    if end_m is not None and end_m > AFTER_HOURS_END:
        flags.append("after hours")
    if (end_m is not None and start_m is not None and end_m < start_m) or event["start_date"] != event["end_date"]:
        flags.append("overnight")

    org_lower = (event["organization"] or "").lower()

    # --- Outside visitors: who's coming to campus, not how many ---
    # Skipped entirely for a setup/teardown meeting: a room being set up or
    # broken down isn't the public event itself (no attendees), and this is
    # exactly the case that was over-flagging the Peace Gala's setup days
    # (2026-10-04 to 2026-10-09) as if 375 people were showing up each day
    # -- confirmed 2026-09-29, see is_setup/is_teardown in load_from_api().
    # A setup/teardown day still carries the "setup/teardown" flag above
    # (secondary tier), so security still knows a crew is on site.
    if not is_setup_or_teardown:
        event_type = event.get("type")
        if event_type == RENTAL_TYPE:
            flags.append("rental")
        elif event_type is None and RENTAL_ORG_HINT in org_lower:
            # CSV mode has no `type` field to confirm this against — org name
            # alone over-flags (Events & Conferences also does internal work),
            # so this is marked as unconfirmed rather than a certain rental.
            flags.append("rental (unconfirmed)")

        # Admissions group tours/visits -- independent of the type/public
        # checks above, since these come through as type "Internal Meeting",
        # public: false, identical to a genuinely internal meeting (see
        # ADMISSIONS_ORG_NAME above). Outside people on campus regardless.
        if ADMISSIONS_ORG_NAME in org_lower and any(h in name_lower for h in VISIT_NAME_HINTS):
            flags.append("campus visit")

        is_public = event.get("public")
        if event_type is not None:
            # Live mode: type is always populated from real Coursedog data
            # (even "" for a blank one, never None), so this is the signal
            # we can trust is_public_facing()'s fuller criterion instead of
            # the raw `public` field alone -- see that function.
            if is_public_facing(event):
                flags.append("public event")
        elif is_public is None and PAC_ORG_NAME in org_lower:
            # CSV mode has no `type`/`public`/`status`-for-is_public_facing
            # fields. PAC is the one org we know well enough to guess
            # confidently: its own setup/teardown work is named as such, so
            # anything else under that org is the show itself.
            flags.append("public event")

    return flags


def load_from_csv(path, target_date):
    events = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            start_date = row["Meeting Start Date"].strip()
            if target_date and start_date != target_date:
                continue
            events.append({
                "name": row["Event Name"].strip(),
                "location": row["Location"].strip(),
                "start_date": start_date,
                "end_date": row["Meeting End Date"].strip(),
                "start_min": parse_time_to_minutes(row["Meeting Start Time"]),
                "end_min": parse_time_to_minutes(row["Meeting End Time"]),
                "organization": row["Organization"].strip(),
                "author_name": row["Author Name"].strip(),
                "author_email": row["Author Email"].strip(),
                "status": row["Event Status"].strip(),
                "public": None,  # not present in the CSV export
                "type": None,    # not present in the CSV export — see RENTAL_ORG_HINT fallback
                "_event_id": None,  # not present in the CSV export -- merge falls back to (name, org)
                "expected_head_count": None,
                "actual_head_count": None,
                "registered_head_count": None,
                # Not present in the CSV export either -- CSV mode falls back
                # to the SETUP_TEARDOWN_PREFIXES name check only. See
                # is_setup/is_teardown in load_from_api() for why the real
                # field matters (confirmed 2026-09-29, Peace Gala).
                "is_setup": False,
                "is_teardown": False,
                # Not present in the CSV export either -- see
                # is_public_facing() below, which only applies its real
                # public-calendar criterion in live mode (where these are
                # populated); CSV mode keeps the older PAC-org heuristic.
                "private": False,
                "is_redacted": False,
            })
    return events


# Confirmed live in build_campus_events.py (Sept 11): skip/limit pagination
# on /meetings silently truncates once a query's *result count* gets large
# enough (a full year came back with 710 meetings and quietly dropped
# everything past ~Dec 10) — it wasn't hitting end-of-data, it was hitting
# some other limit and returning a short page that looked like the end.
# This report's window is small (a handful of days, ~20 events/day) — nowhere
# near that failure's scale — but the pagination loop itself costs nothing to
# include, so it's here defensively rather than assuming a single unpaginated
# GET is safe forever as the window grows.
PAGE_LIMIT = 200


def load_from_api(start_date, end_date):
    """Live pull from Coursedog for the date range [start_date, end_date].
    Requires the dedicated read-only API user's credentials
    (COURSEDOG_READONLY_* env vars, same convention as
    build_campus_events.py) — this script only ever does GET requests, so it
    should never be pointed at a read-write credential."""
    import requests

    base = os.environ.get("COURSEDOG_READONLY_BASE", "https://app.coursedog.com")
    school_id = os.environ.get("COURSEDOG_SCHOOL", "soka_peoplesoft_direct")
    email = os.environ["COURSEDOG_READONLY_EMAIL"]
    password = os.environ["COURSEDOG_READONLY_PASSWORD"]

    session_resp = requests.post(
        f"{base}/api/v1/sessions",
        json={"email": email, "password": password},
        timeout=30,
    )
    session_resp.raise_for_status()
    token = session_resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    rows = []
    skip = 0
    while True:
        resp = requests.get(
            f"{base}/api/v1/em/{school_id}/meetings",
            params={"startDate": start_date, "endDate": end_date, "skip": skip, "limit": PAGE_LIMIT},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        # Confirmed live 2026-09-25: /meetings returns a dict keyed by meeting
        # ID (same pattern as /rooms and /organizations per api-learnings.md),
        # not {data: [...]}. Handle all three shapes Coursedog might hand back.
        if isinstance(payload, list):
            batch = payload
        elif isinstance(payload, dict) and "data" in payload:
            batch = payload["data"]
        elif isinstance(payload, dict):
            batch = list(payload.values())
        else:
            batch = []
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        skip += PAGE_LIMIT

    # Meetings only carry room/org IDs, not display names — same dict-
    # keyed-by-ID shape as meetings itself. Build ID -> name lookups once.
    def id_name_map(resource, name_field="name"):
        r = requests.get(f"{base}/api/v1/em/{school_id}/{resource}", headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        items = data.values() if isinstance(data, dict) and "data" not in data else (
            data["data"] if isinstance(data, dict) else data
        )
        out = {}
        for item in items:
            if isinstance(item, dict):
                key = item.get("_id") or item.get("id")
                out[key] = item.get(name_field) or item.get("displayName") or key
        return out

    room_names = id_name_map("rooms", "displayName")
    org_names = id_name_map("organizations", "name")

    events = []
    for m in rows:
        ev = m.get("eventData", {}) or {}
        # Coursedog redacts eventData entirely for private meetings (no
        # name/org/etc, just {_id, redacted: true}) — that's a deliberate
        # privacy restriction on the read-only account, not missing data.
        # Room and time still come through fine, so still worth a line in
        # the schedule, just labeled honestly instead of "(untitled)".
        is_redacted = bool(ev.get("redacted"))
        room_id = m.get("roomId")
        org_id = ev.get("organization")
        events.append({
            "name": "(private event)" if is_redacted else (ev.get("name") or "(untitled)"),
            "location": room_names.get(room_id, room_id or "-"),
            "start_date": m.get("startDate", start_date),
            "end_date": m.get("endDate", start_date),
            "start_min": _hhmm_to_minutes(m.get("startTime")),
            "end_min": _hhmm_to_minutes(m.get("endTime")),
            "organization": org_names.get(org_id, org_id or "-"),
            "author_name": ev.get("authorName", "-"),
            "author_email": ev.get("authorEmail", "-"),
            "status": m.get("status", ev.get("status", "Confirmed")),
            "public": ev.get("public"),
            "type": ev.get("type"),
            # Groups multiple meetings of the same event (e.g. a multi-stop
            # campus tour booked as separate room reservations) into one
            # card in the outside-visitors summary -- see
            # merge_outside_visitor_groups(). Not present in CSV exports.
            "_event_id": m.get("eventId") or ev.get("_id"),
            # Coursedog has these on the event record but they're often 0 or
            # null in practice -- shown when present and positive, omitted
            # otherwise rather than displaying a misleading "0 attendees".
            "expected_head_count": ev.get("expectedHeadCount"),
            "actual_head_count": ev.get("actualHeadCount"),
            "registered_head_count": ev.get("registeredHeadCount"),
            # Confirmed live 2026-09-29: Coursedog puts real isSetup/
            # isTeardown booleans on the MEETING itself (not just a naming
            # convention). A multi-day setup or teardown block shows up as
            # its own meeting row -- e.g. the Peace Gala's setup was one
            # meeting spanning 2026-10-04 to 2026-10-09, isSetup: true,
            # meeting-level name "" (falls back to the event name, "Soka
            # Peace Gala") -- so it was indistinguishable from the real
            # event day by name, and got flagged "public event" for every
            # day of that range. See classify() for the fix.
            "is_setup": bool(m.get("isSetup")),
            "is_teardown": bool(m.get("isTeardown")),
            "private": bool(ev.get("private")),
            "is_redacted": is_redacted,
        })
    return events


def group_by_date(events, dates):
    """Split a flat event list (as returned by load_from_api across a date
    range) into one list per day, keyed by start_date. dates fixes the
    order and the full set of days to include, even ones with zero events —
    a quiet day is still worth a tab that says so, not a missing tab."""
    by_date = {d: [] for d in dates}
    for e in events:
        if e["start_date"] in by_date:
            by_date[e["start_date"]].append(e)
    return by_date


def _hhmm_to_minutes(val):
    """Coursedog meeting times can come back as int (e.g. 1900) or string."""
    if val is None:
        return None
    if isinstance(val, str):
        if "--" in val or not val.strip():
            return None
        val = int(val)
    h, m = divmod(int(val), 100)
    return h * 60 + m


def attendee_count(event):
    """Best available headcount for an event, or None if nothing usable is
    on record. Coursedog has three headcount fields on the event record
    (expected/actual/registered) but they're frequently 0 or null in
    practice -- actual (what really showed up) beats registered beats
    expected (the least reliable, an early estimate), and a non-positive
    value is treated the same as missing so we never display a misleading
    "0 attendees"."""
    for key in ("actual_head_count", "registered_head_count", "expected_head_count"):
        val = event.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    return None


def merge_outside_visitor_groups(items):
    """Collapse multiple (event, flags) entries that are really one
    underlying event -- e.g. a multi-stop campus tour booked as separate
    room reservations, one meeting per stop -- into a single entry for the
    Outside Visitors summary tier. The FULL SCHEDULE section still lists
    every individual stop; this only thins out the top alert section, per
    request (the alert to security should be shown once).

    Grouping key is the event's _event_id when we have one (live API mode);
    CSV mode has no _event_id, so falls back to (name, organization), which
    is weaker (two same-named events on the same day for the same org would
    merge) but there's no better signal in the CSV export.

    Merged entry keeps the earliest start / latest end across the group,
    the union of flags, a "_stop_count" for display, and the best single
    attendee_count found across the group (max, not summed -- one tour
    group doesn't have its headcount multiplied by its number of stops).
    """
    groups = {}
    order = []
    for e, f in items:
        key = e.get("_event_id") or (e["name"], e["organization"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((e, f))

    merged = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            e, f = group[0]
            e = dict(e)
            e["_attendee_count"] = attendee_count(e)
            merged.append((e, f))
            continue
        starts = [e["start_min"] for e, _ in group if e["start_min"] is not None]
        ends = [e["end_min"] for e, _ in group if e["end_min"] is not None]
        flags = []
        for _, f in group:
            for x in f:
                if x not in flags:
                    flags.append(x)
        counts = [attendee_count(e) for e, _ in group]
        counts = [c for c in counts if c is not None]
        rep = dict(group[0][0])
        rep["start_min"] = min(starts) if starts else rep["start_min"]
        rep["end_min"] = max(ends) if ends else rep["end_min"]
        rep["_stop_count"] = len(group)
        rep["_attendee_count"] = max(counts) if counts else attendee_count(rep)
        merged.append((rep, flags))
    return merged


def build_report(events, target_date):
    active = [e for e in events if classify(e) != ["canceled"]]
    active.sort(key=lambda e: (e["start_min"] if e["start_min"] is not None else -1))
    flagged = [(e, classify(e)) for e in active]
    notable = [(e, f) for e, f in flagged if f]
    outside_visitors = [(e, f) for e, f in notable if any(x in OUTSIDE_VISITOR_FLAGS for x in f)]
    outside_visitors = merge_outside_visitor_groups(outside_visitors)
    secondary = [(e, f) for e, f in notable if not any(x in OUTSIDE_VISITOR_FLAGS for x in f)]
    canceled = [e for e in events if e["status"].lower() in CANCELED_STATUSES]

    lines = []

    def section(title, items):
        lines.append("")
        lines.append(title)
        lines.append("-" * 60)
        for e, f in items:
            lines.append(f"[{', '.join(f).upper()}] {e['name']}")
            lines.append(
                f"    {format_minutes(e['start_min'])} - {format_minutes(e['end_min'])}"
                f"  @ {e['location']}  ({e['organization'] or '-'})"
            )
            extras = []
            if e.get("_attendee_count"):
                extras.append(f"~{e['_attendee_count']} attendees")
            if e.get("_stop_count", 1) > 1:
                extras.append(f"{e['_stop_count']} stops")
            if extras:
                lines.append(f"    {' | '.join(extras)}")
        lines.append("")

    lines.append(f"CAMPUS SECURITY — DAILY EVENT REPORT")
    lines.append(f"{target_date}  ({len(active)} scheduled events)")
    lines.append("=" * 60)

    if outside_visitors:
        section(f"🚪 OUTSIDE VISITORS — {len(outside_visitors)} event(s), non-SUA people on campus", outside_visitors)
    if secondary:
        section(f"⚠ ALSO FLAGGED — {len(secondary)} event(s) worth a second look", secondary)

    lines.append(f"FULL SCHEDULE — {target_date}")
    lines.append("-" * 60)
    for e in active:
        f = classify(e)
        tag = f" [{', '.join(f)}]" if f else ""
        lines.append(
            f"{format_minutes(e['start_min']):>10} - {format_minutes(e['end_min']):<10} "
            f"{e['name']}{tag}"
        )
        lines.append(f"{'':>10}   {e['location']}  |  {e['organization'] or '-'}")

    if canceled:
        lines.append("")
        lines.append(f"CANCELED ({len(canceled)}) — no action needed, listed for awareness")
        lines.append("-" * 60)
        for e in canceled:
            lines.append(f"  {e['name']}  @ {e['location']}")

    return "\n".join(lines), notable, active, canceled


def build_html_report(events, target_date):
    text_report, notable, active, canceled = build_report(events, target_date)
    outside_visitors = [(e, f) for e, f in notable if any(x in OUTSIDE_VISITOR_FLAGS for x in f)]
    outside_visitors = merge_outside_visitor_groups(outside_visitors)

    def esc(s):
        return (s or "-").replace("&", "&amp;").replace("<", "&lt;")

    def flag_badges(f, color="amber"):
        palette = {
            "amber": ("#fde68a", "#78350f"),
            "red": ("#fecaca", "#7f1d1d"),
            "blue": ("#bfdbfe", "#1e3a8a"),
        }
        bg, fg = palette[color]
        return "".join(
            f'<span style="background:{bg};color:{fg};border-radius:4px;'
            f'padding:2px 6px;font-size:11px;margin-right:4px;">{esc(x)}</span>'
            for x in f
        )

    def extra_line(e):
        extras = []
        if e.get("_attendee_count"):
            extras.append(f"~{e['_attendee_count']} attendees")
        if e.get("_stop_count", 1) > 1:
            extras.append(f"{e['_stop_count']} stops")
        if not extras:
            return ""
        return (
            f'<div style="color:#1e3a8a;font-size:12px;font-weight:600;margin-bottom:4px;">'
            f'{" &middot; ".join(extras)}</div>'
        )

    def tier_html(items, heading, border, bg, fg, badge_color):
        if not items:
            return ""
        cards = "".join(f"""
        <div style="background:{bg};border:1px solid {border};border-radius:8px;
                    padding:12px 14px;margin-bottom:8px;">
          <div style="font-weight:700;color:{fg};font-size:15px;">{esc(e['name'])}</div>
          <div style="color:#374151;font-size:13px;margin:2px 0 6px;">
            {format_minutes(e['start_min'])}&ndash;{format_minutes(e['end_min'])}
            &middot; {esc(e['location'])} &middot; {esc(e['organization'])}
          </div>
          {extra_line(e)}
          {flag_badges(f, color=badge_color)}
        </div>""" for e, f in items)
        return f"""
        <h3 style="color:{fg};font-size:15px;margin:16px 0 8px;">{heading}</h3>
        {cards}
        """

    outside_visitors_html = tier_html(
        outside_visitors, f"🚪 Outside visitors — {len(outside_visitors)} event(s), non-SUA people on campus",
        "#93c5fd", "#eff6ff", "#1e3a8a", "blue",
    )

    rows_html = []
    for e in active:
        f = classify(e)
        rows_html.append(f"""
        <tr style="border-bottom:1px solid #e5e7eb;">
          <td style="padding:8px 12px;white-space:nowrap;color:#374151;">
            {format_minutes(e['start_min'])}&ndash;{format_minutes(e['end_min'])}
          </td>
          <td style="padding:8px 12px;">
            <div style="font-weight:600;color:#111827;">{esc(e['name'])}</div>
            <div style="color:#6b7280;font-size:13px;">{esc(e['location'])} &middot; {esc(e['organization'])}</div>
            <div>{flag_badges(f)}</div>
          </td>
        </tr>""")

    canceled_html = ""
    if canceled:
        items = "".join(f"<li>{esc(e['name'])} @ {esc(e['location'])}</li>" for e in canceled)
        canceled_html = f"""
        <h3 style="color:#6b7280;font-size:14px;margin-top:24px;">Canceled ({len(canceled)}) — for awareness only</h3>
        <ul style="color:#9ca3af;font-size:13px;">{items}</ul>"""

    notable_count = len(notable)
    banner = ""
    if notable_count:
        banner = f"""
        <div style="background:#fef3c7;border:1px solid #fbbf24;border-radius:6px;
                    padding:10px 14px;margin-bottom:16px;color:#78350f;font-size:14px;">
          {notable_count} event(s) flagged below for outside visitors, after-hours access,
          or setup/teardown.
        </div>"""

    return f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;">
      <h2 style="color:#111827;margin-bottom:4px;">Campus Security — Daily Event Report</h2>
      <div style="color:#6b7280;margin-bottom:16px;">{target_date} &middot; {len(active)} scheduled events</div>
      {banner}
      {outside_visitors_html}
      <table style="width:100%;border-collapse:collapse;">
        {''.join(rows_html)}
      </table>
      {canceled_html}
    </div>
    """


def _day_label(date_str, today_str):
    """'Today', 'Tomorrow', or 'Wed 10/1' for anything further out."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    delta = (d - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    return d.strftime("%a %-m/%-d")


def build_standalone_page(events_by_date, dates, generated_at):
    """Full HTML page for GitHub Pages publishing — the primary delivery
    method. One tab per day in `dates` (today plus a bounded lookahead
    window, not open-ended date browsing — see project notes on why: a
    wider window is trivial data-load-wise, but it multiplies exposure on
    an unauthenticated URL and near-term future days are only as fresh as
    this run, so unbounded lookahead isn't worth either cost). Reuses
    build_html_report()'s per-day content (built for embedding in an email
    body) and the same Soka brand palette / noindex convention as
    docs/campus-happenings-*/index.html, since this publishes to the same
    unauthenticated-but-unlisted GitHub Pages site.

    Renders plain, tab-free content when there's exactly one day (CSV mode,
    or --days 1) — no reason to show tab UI for a single tab."""
    today_str = dates[0]

    panels = []
    tabs = []
    for i, d in enumerate(dates):
        day_events = events_by_date.get(d, [])
        _, notable, active, _ = build_report(day_events, d)
        outside_count = len(merge_outside_visitor_groups(
            [(e, f) for e, f in notable if any(x in OUTSIDE_VISITOR_FLAGS for x in f)]
        ))
        body = build_html_report(day_events, d)
        active_style = "" if i == 0 else "display:none;"
        panels.append(f'<div class="day-panel" id="day-panel-{i}" style="{active_style}">{body}</div>')

        label = _day_label(d, today_str)
        badge = f' <span class="tab-badge">{outside_count}</span>' if outside_count else ""
        active_class = " active" if i == 0 else ""
        tabs.append(
            f'<button class="day-tab{active_class}" id="day-tab-{i}" '
            f'onclick="showDay({i})">{label}{badge}</button>'
        )

    tab_bar = ""
    if len(dates) > 1:
        tab_bar = f'<div class="tab-bar">{"".join(tabs)}</div>'

    # No zoneinfo/tz-database dependency — Pacific is UTC-7 (PDT) or UTC-8
    # (PST); this only needs to be legible to a person glancing at a
    # timestamp, not exact to the minute, so a fixed PDT offset is fine
    # March-November and off by an hour the rest of the year.
    from datetime import timedelta
    pacific = generated_at - timedelta(hours=7)
    generated_str = (
        f"{generated_at.strftime('%B %-d, %Y')} at "
        f"{pacific.strftime('%-I:%M %p')} Pacific "
        f"({generated_at.strftime('%-I:%M %p')} UTC)"
    )
    freshness_note = (
        "Refreshed automatically once a day — not real-time. Today's tab is "
        "current as of this run; later days are subject to change before "
        "they arrive." if len(dates) > 1 else
        "Refreshed automatically once a day — not real-time."
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Campus Security Report — {today_str}</title>
<meta name="robots" content="noindex, nofollow, noarchive">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Work+Sans:ital,wght@0,400;0,500;0,600;0,700&display=swap">
<style>
  :root{{
    --paper:#FEFDEB;
    --ink:#001D61;
    --ink-muted:#6B7CA3;
    --line:#CCD2DF;
    --teal:#004B87;
  }}
  *{{box-sizing:border-box;}}
  body{{
    margin:0;
    background:var(--paper);
    color:var(--ink);
    font-family:"Work Sans",-apple-system,Segoe UI,Roboto,sans-serif;
    padding:24px 16px 48px;
  }}
  .wrap{{max-width:680px;margin:0 auto;}}
  .tab-bar{{
    display:flex;
    flex-wrap:wrap;
    gap:6px;
    margin-bottom:20px;
    border-bottom:1px solid var(--line);
    padding-bottom:12px;
  }}
  .day-tab{{
    font-family:inherit;
    font-size:13px;
    font-weight:600;
    color:var(--ink-muted);
    background:#fff;
    border:1px solid var(--line);
    border-radius:6px;
    padding:6px 12px;
    cursor:pointer;
  }}
  .day-tab.active{{
    color:#fff;
    background:var(--teal);
    border-color:var(--teal);
  }}
  .tab-badge{{
    display:inline-block;
    background:#fecaca;
    color:#7f1d1d;
    border-radius:999px;
    font-size:11px;
    font-weight:700;
    padding:1px 6px;
    margin-left:4px;
  }}
  .day-tab.active .tab-badge{{
    background:#fff;
    color:var(--teal);
  }}
  .updated{{
    color:var(--ink-muted);
    font-size:13px;
    border-top:1px solid var(--line);
    margin-top:28px;
    padding-top:12px;
  }}
</style>
</head>
<body>
  <div class="wrap">
    {tab_bar}
    {"".join(panels)}
    <div class="updated">Report generated {generated_str}. {freshness_note}</div>
  </div>
  <script>
    function showDay(i) {{
      document.querySelectorAll('.day-panel').forEach(function(el, idx) {{
        el.style.display = (idx === i) ? '' : 'none';
      }});
      document.querySelectorAll('.day-tab').forEach(function(el, idx) {{
        el.classList.toggle('active', idx === i);
      }});
    }}
  </script>
</body>
</html>
"""


def send_email(subject, text_body, html_body):
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("[security_report] SMTP_HOST not set — skipping email send.", file=sys.stderr)
        return
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("SECURITY_EMAIL_FROM", user)
    recipients = [r.strip() for r in os.environ["SECURITY_EMAIL_TO"].split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(sender, recipients, msg.as_string())
    print(f"[security_report] Emailed {len(recipients)} recipient(s).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="Path to a Coursedog export CSV (manual mode, single day only)")
    ap.add_argument("--live", action="store_true", help="Pull live from Coursedog API")
    ap.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD, default today")
    ap.add_argument("--days", type=int, default=1, help="Live mode only: number of days starting at --date to include as tabs (default 1, i.e. just --date)")
    ap.add_argument("--send", action="store_true", help="Actually email the report (else just print). --live mode only; emails just --date, not the full --days window.")
    ap.add_argument("--publish", metavar="PATH", help="Write a standalone HTML page to PATH (e.g. docs/security-XXXX/index.html)")
    args = ap.parse_args()

    from datetime import timedelta
    start = datetime.strptime(args.date, "%Y-%m-%d").date()
    dates = [(start + timedelta(days=i)).isoformat() for i in range(max(args.days, 1))]

    if args.live:
        events = load_from_api(dates[0], dates[-1])
    elif args.csv:
        events = load_from_csv(args.csv, args.date)
        dates = [args.date]  # CSV export is a single-day snapshot; --days doesn't apply
    else:
        print("Specify --csv PATH or --live", file=sys.stderr)
        sys.exit(1)

    events_by_date = group_by_date(events, dates)

    # Always print --date's text report to stdout, regardless of --days.
    text_report, notable, active, canceled = build_report(events_by_date[args.date], args.date)
    print(text_report)

    if args.publish:
        page = build_standalone_page(events_by_date, dates, datetime.now(timezone.utc))
        out_path = Path(args.publish)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(page, encoding="utf-8")
        print(f"[security_report] Published standalone page to {out_path} ({len(dates)} day(s): {dates[0]}..{dates[-1]})")

    if args.send:
        html_report = build_html_report(events_by_date[args.date], args.date)
        subject = f"Campus Security — {args.date} — {len(notable)} flagged / {len(active)} events"
        send_email(subject, text_report, html_report)


if __name__ == "__main__":
    main()
