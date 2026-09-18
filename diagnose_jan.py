#!/usr/bin/env python3
"""One-off diagnostic: does /meetings return anything at all for a narrow
window around the Health Professions workshop (Jan 26, 2027) when queried
directly, vs. our full-year pull which returned nothing past ~Dec 10, 2026?
Isolates whether this is a wide-date-range pagination/query bug."""
import json, os, requests

BASE = os.environ["COURSEDOG_READONLY_BASE"]
EMAIL = os.environ["COURSEDOG_READONLY_EMAIL"]
PASSWORD = os.environ["COURSEDOG_READONLY_PASSWORD"]
SCHOOL = os.environ["COURSEDOG_SCHOOL"]

token = requests.post(f"{BASE}/api/v1/sessions",
    json={"email": EMAIL, "password": PASSWORD}, timeout=30).json()["token"]
headers = {"Authorization": f"Bearer {token}"}

print("=== Narrow query: 2027-01-20 to 2027-02-01 ===")
resp = requests.get(f"{BASE}/api/v1/em/{SCHOOL}/meetings",
    params={"startDate": "2027-01-20", "endDate": "2027-02-01", "skip": 0, "limit": 200},
    headers=headers, timeout=30)
print("status:", resp.status_code)
page = resp.json()
batch = page if isinstance(page, list) else (page.get("data") or page.get("meetings") or list(page.values()) if isinstance(page, dict) else [])
print("count:", len(batch))
for m in batch[:10]:
    ev = m.get("eventData") or {}
    print(" -", m.get("startDate"), "|", ev.get("name"), "|", ev.get("type"), "| public:", ev.get("public"))

print()
print("=== Direct search by name ===")
resp2 = requests.get(f"{BASE}/api/v1/em/{SCHOOL}/events/search/Health%20Professions",
    headers=headers, timeout=30)
print("status:", resp2.status_code)
print(json.dumps(resp2.json(), indent=2, default=str)[:3000])
