#!/usr/bin/env python3
"""
Daily campus-security event report for Soka University.

Two ways to run this:

1. Against a Coursedog export CSV (manual mode):
       python security_report.py --csv path/to/export.csv --date 2026-09-25

2. Live against the Coursedog API (automated mode — what the scheduled
   GitHub Action uses):
       python security_report.py --live --date 2026-09-25

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

# Fallback for CSV mode, where there's no `type` field to check: org name
# is a weaker signal (Events & Conferences also runs some internal-facing
# bookings), so this gets its own distinct, lower-confidence flag rather
# than being treated the same as a confirmed External Rental type match.
RENTAL_ORG_HINT = "events & conferences"

CANCELED_STATUSES = {"canceled", "cancelled", "denied"}

# Tiers, in the order they appear in the report.
OUTSIDE_VISITOR_FLAGS = {"rental", "rental (unconfirmed)", "public event"}


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


def classify(event):
    """Return a list of flag strings for a single event/meeting row."""
    flags = []
    name_lower = event["name"].lower()
    status_lower = event["status"].lower()

    if status_lower in CANCELED_STATUSES:
        flags.append("canceled")
        return flags  # don't bother with other flags on a canceled event

    if any(name_lower.startswith(p) for p in SETUP_TEARDOWN_PREFIXES):
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
    if "development" in org_lower or "alumni" in org_lower:
        flags.append("VIP/donor-facing")

    # --- Outside visitors: who's coming to campus, not how many ---
    event_type = event.get("type")
    if event_type == RENTAL_TYPE:
        flags.append("rental")
    elif event_type is None and RENTAL_ORG_HINT in org_lower:
        # CSV mode has no `type` field to confirm this against — org name
        # alone over-flags (Events & Conferences also does internal work),
        # so this is marked as unconfirmed rather than a certain rental.
        flags.append("rental (unconfirmed)")

    is_public = event.get("public")
    if is_public is True:
        # Live-mode signal, any org — anything genuinely open to non-SUA
        # people, not just PAC.
        flags.append("public event")
    elif is_public is None and PAC_ORG_NAME in org_lower:
        # CSV mode has no `public` field. PAC is the one org we know well
        # enough to guess confidently: its own setup/teardown work is
        # named as such, so anything else under that org is the show
        # itself.
        is_internal_looking = any(name_lower.startswith(p) for p in SETUP_TEARDOWN_PREFIXES)
        if not is_internal_looking:
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
            })
    return events


def load_from_api(target_date):
    """Live pull from Coursedog. Requires the dedicated read-only API user's
    credentials (COURSEDOG_READONLY_* env vars, same convention as
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

    resp = requests.get(
        f"{base}/api/v1/em/{school_id}/meetings",
        params={"startDate": target_date, "endDate": target_date},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    # Confirmed live 2026-09-25: /meetings returns a dict keyed by meeting
    # ID (same pattern as /rooms and /organizations per api-learnings.md),
    # not {data: [...]}. Handle all three shapes Coursedog might hand back.
    if isinstance(payload, list):
        rows = payload
    elif "data" in payload:
        rows = payload["data"]
    else:
        rows = list(payload.values())

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
            "start_date": m.get("startDate", target_date),
            "end_date": m.get("endDate", target_date),
            "start_min": _hhmm_to_minutes(m.get("startTime")),
            "end_min": _hhmm_to_minutes(m.get("endTime")),
            "organization": org_names.get(org_id, org_id or "-"),
            "author_name": ev.get("authorName", "-"),
            "author_email": ev.get("authorEmail", "-"),
            "status": m.get("status", ev.get("status", "Confirmed")),
            "public": ev.get("public"),
            "type": ev.get("type"),
        })
    return events


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


def build_report(events, target_date):
    active = [e for e in events if classify(e) != ["canceled"]]
    active.sort(key=lambda e: (e["start_min"] if e["start_min"] is not None else -1))
    flagged = [(e, classify(e)) for e in active]
    notable = [(e, f) for e, f in flagged if f]
    outside_visitors = [(e, f) for e, f in notable if any(x in OUTSIDE_VISITOR_FLAGS for x in f)]
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


def build_standalone_page(events, target_date, generated_at):
    """Full HTML page for GitHub Pages publishing — the primary delivery
    method. Wraps build_html_report()'s content (built for embedding in an
    email body) in a real document, using the same Soka brand palette and
    noindex convention as docs/campus-happenings-*/index.html, since this is
    published to the same unauthenticated-but-unlisted GitHub Pages site and
    should look like it belongs there."""
    body = build_html_report(events, target_date)
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

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Campus Security Report — {target_date}</title>
<meta name="robots" content="noindex, nofollow, noarchive">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Work+Sans:ital,wght@0,400;0,500;0,600;0,700&display=swap">
<style>
  :root{{
    --paper:#FEFDEB;
    --ink:#001D61;
    --ink-muted:#6B7CA3;
    --line:#CCD2DF;
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
    {body}
    <div class="updated">Report generated {generated_str}. Refreshed automatically once a day — not real-time.</div>
  </div>
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
    ap.add_argument("--csv", help="Path to a Coursedog export CSV (manual mode)")
    ap.add_argument("--live", action="store_true", help="Pull live from Coursedog API")
    ap.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD, default today")
    ap.add_argument("--send", action="store_true", help="Actually email the report (else just print)")
    ap.add_argument("--publish", metavar="PATH", help="Write a standalone HTML page to PATH (e.g. docs/security-XXXX/index.html)")
    args = ap.parse_args()

    if args.live:
        events = load_from_api(args.date)
    elif args.csv:
        events = load_from_csv(args.csv, args.date)
    else:
        print("Specify --csv PATH or --live", file=sys.stderr)
        sys.exit(1)

    text_report, notable, active, canceled = build_report(events, args.date)
    print(text_report)

    if args.publish:
        page = build_standalone_page(events, args.date, datetime.now(timezone.utc))
        out_path = Path(args.publish)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(page, encoding="utf-8")
        print(f"[security_report] Published standalone page to {out_path}")

    if args.send:
        html_report = build_html_report(events, args.date)
        subject = f"Campus Security — {args.date} — {len(notable)} flagged / {len(active)} events"
        send_email(subject, text_report, html_report)


if __name__ == "__main__":
    main()
