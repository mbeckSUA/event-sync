#!/usr/bin/env python3
"""
PAC Events → Coursedog Sync

Fetches iCal feed from performingarts.soka.edu and pushes events to Coursedog.

Requires these environment variables:
  COURSEDOG_BASE        e.g. https://staging.coursedog.com
  COURSEDOG_EMAIL
  COURSEDOG_PASSWORD
  COURSEDOG_SCHOOL      soka_peoplesoft_direct
  PAC_ORG_ID            IHK4ja911DtXuRP80OTd

Update logic:
  - New events: full description (first paragraph + URL)
  - Existing events: name, dates, times updated; description preserved
"""

import json, logging, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from icalendar import Calendar

FEED_URL = "https://www.performingarts.soka.edu/feeds/pac-events"
PACIFIC = ZoneInfo("America/Los_Angeles")
STATE_FILE = Path(__file__).parent / "pac_state.json"
DEFAULT_DURATION_HOURS = 2

COURSEDOG_BASE = os.environ["COURSEDOG_BASE"]
COURSEDOG_EMAIL = os.environ["COURSEDOG_EMAIL"]
COURSEDOG_PASSWORD = os.environ["COURSEDOG_PASSWORD"]
COURSEDOG_SCHOOL = os.environ["COURSEDOG_SCHOOL"]
PAC_ORG_ID = os.environ["PAC_ORG_ID"]

ROOM_MAP = {
    "campus green":                        "g8Kzp4Z2vlKjqGmN4xT0",
    "concert hall":                        "5990bde8-436c-4faa-bab5-4e7d8ff030fa",
    "performing arts center concert hall": "5990bde8-436c-4faa-bab5-4e7d8ff030fa",
    "black box theater":                   "ghbsBrBipuAn45Drp5Pw",
    "black box theatre":                   "ghbsBrBipuAn45Drp5Pw",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

def get_token():
    resp = requests.post(f"{COURSEDOG_BASE}/api/v1/sessions",
        json={"email": COURSEDOG_EMAIL, "password": COURSEDOG_PASSWORD}, timeout=30)
    resp.raise_for_status()
    return resp.json()["token"]

def to_pacific(dt):
    if not isinstance(dt, datetime):
        dt = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PACIFIC)

def first_paragraph(text, url):
    """Return first non-empty paragraph of text, with event URL appended."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    excerpt = paragraphs[0] if paragraphs else text.strip()
    if url:
        excerpt += f"\n\nMore info: {url}"
    return excerpt

def fetch_feed():
    resp = requests.get(FEED_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.content)
    events = []
    now = datetime.now(tz=PACIFIC)
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        url = str(component.get("URL", "")).strip()
        if not url:
            log.warning(f"Skipping event with no URL: {component.get('SUMMARY')}")
            continue
        uid = url  # use URL as stable dedup key
        dtstart = to_pacific(component.get("DTSTART").dt)
        dtend = to_pacific(component.get("DTEND").dt)
        if dtend < now:
            continue
        if dtstart == dtend:
            dtend = dtstart + timedelta(hours=DEFAULT_DURATION_HOURS)
            log.warning(f"Zero-duration fixed: {component.get('SUMMARY')} — end set to {dtend}")
        location = str(component.get("LOCATION", "")).replace("\\,", ",").replace("&amp;", "&").strip()
        raw_description = str(component.get("DESCRIPTION", "")).replace("\\n", "\n").strip()
        events.append({
            "uid": uid,
            "summary": str(component.get("SUMMARY", "")).strip(),
            "description": first_paragraph(raw_description, url),
            "location": location,
            "url": url,
            "dtstart": dtstart,
            "dtend": dtend,
        })
    log.info(f"Fetched {len(events)} upcoming events from PAC feed")
    return events

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def to_hhmm(dt):
    return dt.hour * 100 + dt.minute

def resolve_room(location):
    for key, room_id in ROOM_MAP.items():
        if key in location.lower():
            return room_id
    return None

def get_current_description(token, coursedog_id):
    """Fetch existing event description from Coursedog to preserve manual edits."""
    resp = requests.get(
        f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/events/{coursedog_id}",
        headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.ok:
        return resp.json().get("description", "")
    return ""

def build_payload(event, description):
    meeting = {
        "startDate": event["dtstart"].strftime("%Y-%m-%d"),
        "endDate": event["dtend"].strftime("%Y-%m-%d"),
        "startTime": to_hhmm(event["dtstart"]),
        "endTime": to_hhmm(event["dtend"]),
        "allDay": False,
        "status": "Confirmed",
    }
    room_id = resolve_room(event["location"])
    if room_id:
        meeting["roomId"] = room_id
    return {
        "name": event["summary"],
        "type": "Campus Events",
        "description": description,
        "extendedDescription": f'<a href="{event["url"]}">More info and tickets</a>' if event["url"] else "",
        "organization": PAC_ORG_ID,
        "status": "Confirmed",
        "public": True,
        "meetings": [meeting],
    }

def create_event(token, payload):
    resp = requests.post(
        f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/events?doPutMeetings=true",
        json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    return resp.json()

def update_event(token, coursedog_id, payload):
    resp = requests.put(
        f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/events/{coursedog_id}?doPutMeetings=true",
        json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()

def main():
    token = get_token()
    log.info("Authenticated with Coursedog")
    events = fetch_feed()
    state = load_state()
    created = updated = errors = 0

    for event in events:
        uid = event["uid"]
        try:
            if uid in state:
                # Preserve existing description — fetch it from Coursedog
                current_description = get_current_description(token, state[uid])
                payload = build_payload(event, current_description)
                update_event(token, state[uid], payload)
                updated += 1
                log.info(f"Updated:  {event['summary']} ({event['dtstart'].date()})")
            else:
                # New event — use description from feed
                payload = build_payload(event, event["description"])
                coursedog_id = create_event(token, payload)
                state[uid] = coursedog_id
                created += 1
                log.info(f"Created:  {event['summary']} ({event['dtstart'].date()}) → {coursedog_id}")
        except requests.HTTPError as e:
            log.error(f"Error on '{event['summary']}': {e.response.status_code} {e.response.text}")
            errors += 1

    save_state(state)
    log.info(f"Done — created: {created}, updated: {updated}, errors: {errors}")
    if errors:
        sys.exit(1)

if __name__ == "__main__":
    main()