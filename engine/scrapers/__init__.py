"""
Event scrapers — the EXTRACTION adapters, split into platform-family modules
(2026-08-29; one file per family so an agent reads core + its family instead of
grepping a 191KB module — this layout deliberately diverges from Chicago's
single-file scrapers.py). Each adapter takes a source's endpoint + a [start,end]
date window and returns a list of normalized event dicts (`core.norm`). Adapters
are transport-typed, not venue-typed: one `scrape_squarespace` serves every
Squarespace venue — the "meta scraper, city to city" idea.

Layout (import surface is unchanged — everything is re-exported here, and
`ADAPTERS` still dispatches on `PipelineSource.adapter`):
  core.py         shared helpers: fetch/fetch_json (tests patch THESE), UA,
                  LOCAL_TZ, strip_html, route_tag, norm, validate_events
  wordpress.py    tribe_wp, wp_jsonld_pages, craftpeak
  squarespace.py  squarespace
  feeds.py        localist, bibliocommons, wix, ical, google_calendar, luma, nps_api
  jsonld.py       html_jsonld, html_jsonld_paged, bentobox
  eventbrite.py   eventbrite, eventbrite_organizer
  ticketing.py    crowdwork, agile_feed, eventive, ticketleap,
                  ticketmaster_api, beventi_vendor, xtrulink,
                  signup_software
  algolia.py      algolia_events (client-side Algolia index)
  <city>.py       put city-bespoke one-off adapters in their own module
  templates.py    Chicago: chicago_parks (Park District), do312 (DoStuff)

Smoke-test one adapter (was `python scrapers.py ...`):
  .venv/bin/python -m scrapers <adapter> <url> [days]
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urljoin, urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from . import core
from .core import (
    UA, LOCAL_TZ, fetch, fetch_json, strip_html, host_root, _slug, route_tag,
    _with_route, norm, _in_range, valid_event, validate_events,
)
from .wordpress import (
    scrape_tec, _EVO_DATA_TIME, _LOOSE_ISO,
    _loose_when, _epoch_wall, scrape_wp_jsonld_pages,
    scrape_craftpeak, _cp_when, _cp_clock, _cp_detail, _cp_body,
)
from .squarespace import (
    _ms_to_local, _squarespace_item, scrape_squarespace, )
from .feeds import (
    scrape_localist, scrape_bibliocommons, WIX_EVENTS_APP_ID, _wix_instance,
    _wix_local_datetime, scrape_wix, scrape_ical, _GCAL_DAYS, _gcal_parse_dt, _gcal_occurrences, scrape_google_calendar, _NPS_TIME, _nps_hhmm, _nps_true, scrape_nps_api, _LUMA_CAL_ID, _luma_local, _luma_cost, scrape_luma,
)
from .jsonld import (
    _jsonld_blocks, _HUMAN_DATE, _jsonld_localize, _jsonld_when,
    _is_event_type, scrape_html_jsonld, scrape_html_jsonld_paged, _BB_EVENT_HREF, scrape_bentobox,
)
from .eventbrite import (
    EB_BLOB, scrape_eventbrite, _EB_ORG_KEY, _eb_json_array,
    scrape_eventbrite_organizer, )
from .ticketing import (
    scrape_crowdwork, AGILE_BUILDINGS, scrape_agile_feed, _eventive_local,
    scrape_eventive, _tl_listings, _tl_dt, scrape_ticketleap,
    scrape_ticketmaster, _devalue, scrape_beventi_vendor, _XTL_DAY,
    _XTL_NAME_LINK, _XTL_TIME, _XTL_IMG, _XTL_DESC, _xtl_hhmm,
    scrape_xtrulink, _SS_STORE, _ss_local, scrape_signup_software,
    scrape_venuepilot, _VP_QUERY, _vp_time,
)
from .algolia import (
    _alg_dt, _pick as _alg_pick, scrape_algolia_events,
)
from .templates import (
    scrape_chicago_parks, scrape_do312,
)

# ---------------- registry ----------------
# adapter name -> fn(url, source_name, start, end) ; ingest.py dispatches on PipelineSource.adapter
ADAPTERS = {
    # portable, city-agnostic
    "tribe_wp": scrape_tec,
    "squarespace": scrape_squarespace,
    "localist": scrape_localist,
    "bibliocommons": scrape_bibliocommons,
    "wix": scrape_wix,
    "ical": scrape_ical,
    # 2026-09-06 (Max): public Google Calendar ICS *with* RRULE expansion —
    # venue weeklies live as one open-ended recurring VEVENT (wissahickon).
    "google_calendar": scrape_google_calendar,
    # 2026-08-20 (Max): lu.ma public calendar API — portable, calendar id in url
    "luma": scrape_luma,
    # 2026-10-12 (Max): nps_api = National Park Service events API, any
    # ?parkCode= url (Independence NHP first). dates[] expanded one row per
    # date (same-day slots folded — see adapter); DEMO_KEY unless keyed.
    "nps_api": scrape_nps_api,
    "html_jsonld": scrape_html_jsonld,
    # 2026-09-06 (Max): BentoBox restaurant CMS (fake-Z startDate — see adapter)
    "bentobox": scrape_bentobox,
    "ticketmaster_api": scrape_ticketmaster,
    # Philadelphia-shaped, written 2026-08-16 (Job D). eventbrite/free_library/
    # do215 emit routing tags (see route_tag) because their sources are aggregators.
    "eventbrite": scrape_eventbrite,
    # 2026-08-23 (Max): organizer-profile variant — one producer, many rooms.
    "eventbrite_organizer": scrape_eventbrite_organizer,
    "crowdwork": scrape_crowdwork,
    # 2026-08-16 (Max): agile_feed is a generic Agile WebSales aggregator
    # (screens collapsed to buildings via AGILE_BUILDINGS); PFS first.
    "agile_feed": scrape_agile_feed,
    # 2026-08-16 (Max): eventive is a generic Eventive-platform aggregator
    # (routes on venue.name); cinéSPEAK first (Philly source "cinespeak").
    "eventive": scrape_eventive,
    # 2026-08-17 (Max): ticketleap is a generic per-organizer platform adapter
    # (embedded listings JSON, bracket-matched); Mouth Of Rain (group) first.
    "ticketleap": scrape_ticketleap,
    # 2026-08-17 (Max): wp_jsonld_pages is the two-step WP REST list -> per-
    # event-page JSON-LD shape (EventON lite first: Giovanni's Room).
    "wp_jsonld_pages": scrape_wp_jsonld_pages,
    # 2026-08-28 (Max): beventi_vendor = Beventi vendor profile via its Nuxt 3
    # `_payload.json` (devalue flat array, depth-capped resolver — the graph has
    # cycles). Mini-aggregator: routes on the event's own venue_name.
    "beventi_vendor": scrape_beventi_vendor,
    # 2026-08-29 (Max): xtrulink = generic ticketapp.org month-grid calendar
    # ({m}/{y} placeholders in the source url; Woodmere Art Museum first).
    # html_jsonld_paged = inline-JSON-LD listing walked page=1..N
    # (DesignPhiladelphia festival first).
    "xtrulink": scrape_xtrulink,
    # 2026-08-30 (Max): signup.software storefront — one unauthenticated
    # POST /query/event (Prisma shape); config rides the store url query string.
    "signup_software": scrape_signup_software,
    "html_jsonld_paged": scrape_html_jsonld_paged,
    # 2026-09-06 (Max): algolia_events = a site whose events list is an Algolia
    # index queried client-side (public search key compiled into its JS bundle).
    # Credentials + facet filters ride the source url. Museum of the American
    # Revolution first (Philly source "amrevmuseum").
    "algolia_events": scrape_algolia_events,
    # 2026-09-06 (Max): venuepilot = the VenuePilot box-office public GraphQL
    # (paginatedEvents; query lifted from the shipped widget bundle —
    # introspection is disabled). accountIds ride the source url. Generic per
    # account: Claude's Comedy Club first (Philly source "claudescomedy").
    "venuepilot": scrape_venuepilot,
    # 2026-09-06 (Max): craftpeak = the Craftpeak brewery WP platform. No REST,
    # no ical, no Event JSON-LD — taxonomy listing -> per-event detail fetch,
    # with the DATE read off the url slug so only in-window events fan out.
    # Other Half Brewing Philadelphia first (Philly source "otherhalfphilly").
    "craftpeak": scrape_craftpeak,
    # Chicago (from the June 2026 Chicago engine; smoke-tested 2026-10-15)
    "chicago_parks": scrape_chicago_parks,
    "do312": scrape_do312,
}

