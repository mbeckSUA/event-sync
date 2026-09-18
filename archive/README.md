# Archived: PAC website → Coursedog sync

`pac_sync.py` (and its `pac_state.json` dedup map) pulled the iCal feed from
performingarts.soka.edu and pushed events into Coursedog, matching on event
URL to avoid duplicates.

## Why this was archived (2026-09-18)

Renee (PAC director) explained that PAC's own booking process makes a
website-driven sync structurally unworkable: touring shows go through
first/second contractual holds on one or more candidate dates a year to a
year and a half out, narrowed to a single confirmed date roughly nine
months to a year out. The show doesn't go on PAC's own website until right
before the public announcement — the very end of that process, long after
the room needs to already be held in Coursedog.

A sync that waits for the website to create the Coursedog record is always
too late: by the time a show is public, Renee has already needed the room
blocked for the better part of a year. The one case where this sync
appeared to work cleanly (Parnassus Society: La Bohème) was a fluke of
sequencing — Coursedog only entered the picture after that show was
already announced — not evidence the model generalizes.

Decision: Renee enters and manages her own holds directly in Coursedog
(multiple candidate dates as needed, released as the booking narrows),
the same way she used to on the old portal calendar. No automated sync
recreates or reconciles those records. A reverse flow (Coursedog →
PAC's website) was considered and rejected — see project notes
(`pac-event-categorization.md`) for the reasoning: minimal manual burden
saved, real risk of leaking not-yet-announced hold dates publicly, and
value in PAC's site staying independently maintained for SEO/promotion.

## Known issues in this code, if ever revisited

- `PAC_ORG_ID` was hardcoded to `IHK4ja911DtXuRP80OTd`, since confirmed as
  a dead/stale organization ID (see `pac-event-categorization.md`). Any
  events this script created are likely invisible to org-based filtering
  regardless of other fields. Not fixed here since the script is retired.
- Whether *outside organizations* renting PAC space (not PAC's own
  produced/presented shows) follow the same long-hold process, or book
  closer to the date, is still an open question — if they book faster,
  something like this sync could have a narrower, legitimate use there.
  Worth revisiting only if that turns out to be true.
