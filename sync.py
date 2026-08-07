#!/usr/bin/env python3
import json, logging, os, re, sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from icalendar import Calendar

PACIFIC = ZoneInfo("America/Los_Angeles")
HOME_CITY = "Aliso Viejo"
STATE_FILE = Path(__file__).parent / "state.json"

COURSEDOG_BASE = os.environ["COURSEDOG_BASE"]
COURSEDOG_EMAIL = os.environ["COURSEDOG_EMAIL"]
COURSEDOG_PASSWORD = os.environ["COURSEDOG_PASSWORD"]
COURSEDOG_SCHOOL = os.environ["COURSEDOG_SCHOOL"]
COURSEDOG_ORG_ID = os.environ["COURSEDOG_ORG_ID"]

FEED_CONFIG = [
    {"url": "http://sokaathletics.com/calendar.ashx/calendar.ics?sport_id=3",
     "room_env": "COURSEDOG_SOCCER_ROOM_ID", "format": "game"},
    {"url": "https://sokaathletics.com/calendar.ashx/calendar.ics?sport_id=6",
     "room_env": "COURSEDOG_SOCCER_ROOM_ID", "format": "game"},
    {"url": "https://sokaathletics.com/calendar.ashx/calendar.ics?sport_id=9",
     "room_env": "COURSEDOG_VOLLEYBALL_ROOM_ID", "format": "game"},
    {"url": "https://sokaathletics.com/calendar.ashx/calendar.ics?sport_id=4",
     "room_env": "COURSEDOG_POOL_ROOM_ID", "format": "meet"},
    {"url": "https://sokaathletics.com/calendar.ashx/calendar.ics?sport_id=5",
     "room_env": "COURSEDOG_TRACK_ROOM_ID", "format": "meet"},
]

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

def fetch_feed(config):
    url = config["url"]
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.content)
    events = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        uid = str(component.get("UID", "")).strip()
        if not uid:
            continue
        location = str(component.get("LOCATION", "")).replace("\\,", ",").strip()
        events.append({
            "uid": uid,
            "summary": str(component.get("SUMMARY", "")).strip(),
            "description": str(component.get("DESCRIPTION", "")).replace("\\n", " ").strip(),
            "location": location,
            "room_env": config["room_env"],
            "title_format": config["format"],
            "dtstart": to_pacific(component.get("DTSTART").dt),
            "dtend": to_pacific(component.get("DTEND").dt),
        })
    log.info(f"Fetched {len(events)} events from {url}")
    return events

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def is_home_game(location):
    return HOME_CITY.lower() in location.lower()

def is_future(dtend):
    return dtend >= datetime.now(PACIFIC)

def to_hhmm(dt):
    return dt.hour * 100 + dt.minute

def clean_summary(summary):
    text = summary.strip()
    text = re.sub(r'^\[\w+\]\s*', '', text)
    text = text.replace("Soka University ", "").strip()
    return text

def build_title(clean, title_format):
    if title_format == "meet" and " vs " in clean:
        sport_part, meet_part = clean.split(" vs ", 1)
        return f"{sport_part} hosts the {meet_part}"
    return clean

def build_payload(event):
    parts = []
    if event["description"]:
        parts.append(event["description"])
    if event["location"]:
        parts.append(f"Location: {event['location']}")
    full_description = " | ".join(p.strip() for p in parts if p.strip())

    clean = clean_summary(event["summary"])
    title = build_title(clean, event["title_format"])

    meeting = {
        "startDate": event["dtstart"].strftime("%Y-%m-%d"),
        "endDate": event["dtend"].strftime("%Y-%m-%d"),
        "startTime": to_hhmm(event["dtstart"]),
        "endTime": to_hhmm(event["dtend"]),
        "allDay": False,
        "status": "Confirmed",
    }
    room_id = os.environ.get(event["room_env"])
    if is_home_game(event["location"]) and room_id:
        meeting["roomId"] = room_id

    return {
        "name": title,
        "type": "Campus Events",
        "description": full_description,
        "organization": COURSEDOG_ORG_ID,
        "status": "Confirmed",
        "public": True,
        "meetings": [meeting],
    }

def create_event(token, payload):
    resp = requests.post(f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/events?doPutMeetings=true",
        json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    return resp.json()

def update_event(token, coursedog_id, payload):
    resp = requests.put(f"{COURSEDOG_BASE}/api/v1/em/{COURSEDOG_SCHOOL}/events/{coursedog_id}?doPutMeetings=true",
        json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()

def main():
    token = get_token()
    log.info("Authenticated with Coursedog")
    all_events = []
    for config in FEED_CONFIG:
        all_events.extend(fetch_feed(config))
    state = load_state()
    created = updated = errors = skipped = 0
    for event in all_events:
        if not is_home_game(event["location"]):
            continue
        if not is_future(event["dtend"]):
            skipped += 1
            continue
        payload = build_payload(event)
        uid = event["uid"]
        try:
            if uid in state:
                update_event(token, state[uid], payload)
                updated += 1
                log.info(f"Updated:  {payload['name']} ({event['dtstart'].date()})")
            else:
                coursedog_id = create_event(token, payload)
                state[uid] = coursedog_id
                created += 1
                log.info(f"Created:  {payload['name']} ({event['dtstart'].date()}) → {coursedog_id}")
        except requests.HTTPError as e:
            log.error(f"Error on '{payload['name']}': {e.response.status_code} {e.response.text}")
            errors += 1
    save_state(state)
    log.info(f"Done — created: {created}, updated: {updated}, skipped (past): {skipped}, errors: {errors}")
    if errors:
        sys.exit(1)

if __name__ == "__main__":
    main()
