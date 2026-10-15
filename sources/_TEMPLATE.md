# <Source Name>

> One source doc per `pipeline_sources` row. The durable home for a source's
> knowledge so nobody re-researches the platform from scratch. Scout writes the
> research + verdict; Max appends the build result. Filename = the source id.

| field | value |
|---|---|
| **source id** | `<source-id>` |
| **status** | none \| planned \| built |
| **health** | ok \| no_events \| http_error \| parse_error \| — (built sources only) |
| **adapter** | `<adapter>` (or — if not built) |
| **is_aggregator** | yes / no |
| **url** | <the working endpoint ingest actually hits> |
| **covers** | <venue/group, or "many — see mapping" for aggregators> |
| **last verified** | YYYY-MM-DD |

## Platform
What is this site running? (Tribe/WP, Squarespace, Localist, BiblioCommons, Wix,
iCal, JSON-LD, Eventbrite, SeeTickets, Drupal, bespoke, …) How did you tell?

## Endpoint & event path
The exact URL that returns events, and the JSON/HTML path to the event list +
the fields used (name, date, end, venue, url). Paste the smallest working example.

## Venue mapping
How an event maps to a venue. Single-venue: trivial (all events → the one venue).
Aggregator: the per-event key (e.g. `park:<slug>` in `coverage.url`) and how the
fan-out routes. THIS is the load-bearing part for aggregators.

## Gotchas
Cloudflare, User-Agent required, pagination shape, recurring-event explosion,
timezone, past-vs-upcoming split, rate limits, anything that bit us.

## Verdict (Scout)
**GO** or **NO-GO**, and the specific reason. For NO-GO record the dead-end class
(social-only / Cloudflare / JS-only-no-API / rental / prose / empty-feed) so it's
not re-litigated.

## Build log (Max)
- YYYY-MM-DD — adapter `<x>`, gate result (N valid events / why it failed),
  status set. Append over time; never delete history.
