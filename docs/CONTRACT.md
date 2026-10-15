# data.json — contract v1

**This file is the only seam between the engine and the site.**

`engine/export.py` writes `docs/data.json`. The site's page scripts read it. Neither
side may reach past this document. The engine can be rewritten in another language, the
site can be rebuilt in another idiom — as long as both honor contract v1, they keep
working.

- **Producer:** `engine/export.py` (ported from `event_wizard_chicago/export.py`; the
  bundle is byte-compatible with Chicago's, so tooling is shared).
- **Consumer:** `docs/app.js` (the shared core) plus one script per surface —
  `bulletin.js` (index.html: ranked list + map behind one toggle), `calendar.js`,
  `sources.js` (sources.html), `venues.js` (venues.html), `event.js` — and the
  `source.html` / `venue.html` / `event.html` detail pages (which select one
  record via an `?id=` query parameter).
- **Transport:** a single JSON file, `docs/data.json`, fetched with a relative path from
  the same directory. No API, no build step, no query language.
- **Second file:** `docs/hoods.geojson` — see [Neighborhoods](#neighborhoods).

- **Off to the side:** `docs/funnel.json` is NOT part of this bundle. It is the
  pipeline snapshot written by `engine/funnel.py` (and by every export), rendered by
  `docs/funnel.html` (noindex).
- **Derived outputs:** the same export pass also writes the agent-facing renditions
  (`data.json` is too large for assistant fetch tools; all advertised by
  `docs/llms.txt`): `docs/today.txt` (the entry point — instructions, today's
  listing inline, links to the day files), `docs/d/<YYYY-MM-DD>.txt` (one small
  file per day in the window; the directory is rewritten wholesale each pass),
  `docs/week.txt` (the whole 7 days in one file), `docs/week.md` (byte-identical
  twin; `.txt` is canonical because Pages serves it as `text/plain`, which every
  assistant fetcher ingests, where `text/markdown` is sometimes refused) and
  `docs/sitemap.xml` (the static pages, the text surfaces above — the only URLs
  that carry data on a raw fetch, since the HTML is client-rendered — and every
  `event.html?id=` permalink; referenced from `docs/robots.txt`). All are pure renditions of this bundle —
  derived data, not part of the contract's JSON shape, never read by the site.

## Versioning

This is **contract v1**. Rules:

- **Adding** a key to an object is a *minor* change. The site must ignore unknown keys.
- **Removing or retyping** a key is a *breaking* change. It requires contract v2, a note
  in this file, and a site change in the same commit.
- **Nullability is part of the type.** A field documented `string|null` may be null in any
  export; the site must render without it. Widening a non-null field to nullable is
  breaking.
- **Date fields are not uniform.** `events[].date` is a bare `YYYY-MM-DD`; `generated` and
  `series[].first_date`/`last_date` are full ISO datetimes. Every consumer reduces to a day
  key before comparing. Don't "fix" this without a version bump — the datetimes are what
  keeps the bundle byte-compatible with Chicago's export.
- The site never assumes a collection is present. `bundle.series` in particular is absent
  from pre-v1 fixtures and is read as `bundle.series || []`.

## Top level

```json
{ "generated": "...", "city": "...", "weights": {...}, "meta": {...},
  "sources": [...], "groups": [...], "venues": [...], "events": [...], "series": [...] }
```

| key | type | notes |
|---|---|---|
| `generated` | `string` | ISO 8601 local datetime of the export, e.g. `2026-08-16T19:16:58.608782`. No timezone suffix. Site renders it as a date only. |
| `city` | `string` | `"Chicago"` (city.json). |
| `weights` | `object` | dim name → weight (`number`, 0–1). The six keys below; sums to ~1.0. Drives the composite and the display order of dimensions in the detail overlay. |
| `meta` | `object` | Counts, for the ops view and the footer. Advisory only — **never** the basis of a render decision; the site counts the arrays itself. |
| `sources` | `array` | Where events come from. Drives the source board (`sources.html`) and per-source pages (`source.html?id=`), joined to events via `events[].source_id` and to venues via `venues[].feeds[].source_id`. |
| `groups` | `array` | Presenters/organizers. Drives `groups.html` / `group.html?id=`. |
| `venues` | `array` | See below. Drives the map and per-venue pages (`venue.html?id=`). |
| `events` | `array` | See below. The product. |
| `series` | `array` | See below. **May be absent** in older exports. |

### `weights`

Exactly these six keys, which are also the six score dimensions:

`social_quality`, `serendipity`, `uniqueness`, `community_building`,
`solo_friendliness`, `value_for_price`

### `meta`

`events`, `scored`, `venues`, `sources`, `flyers`, `duplicates_suppressed`,
`invisible_suppressed`, `unlisted_suppressed`, `scrapers_built`, `scrapers_todo`,
`venues_no_pipeline`, `venues_with_feeds`, `feed_links`, `groups`, `series` — all
`number`. `series` is absent when `series` is absent. Additive; treat every key as
optional.

`duplicates_suppressed` counts same-real-venue/day event observations retained in the DB
but omitted from this export by the conservative cross-source identity layer. Storage is
source-scoped; a source observation is evidence even when the public site serves another
source's version of that event.

`invisible_suppressed` counts in-window events held back by the visibility rule (the
engine's `is_visible`): events at un-vetted discovered venues or from un-monitored
groups stay in the DB but off the site until a human/agent vets the target.

`unlisted_suppressed` counts in-window events that vanished from their own feed on a
re-pull (renamed/cancelled/deleted upstream). Ingest's window-aware retirement marks
them `lifecycle='unlisted'`; they stay in the DB, come back if re-seen, and are never
exported while unlisted.

## `events[]`

The unit of the product. One row per event *instance* (a weekly series produces many).

| field | type | notes |
|---|---|---|
| `id` | `number` | Stable within one export only. Used as the DOM key, the overlay target, and `event.html?id=` permalinks — which therefore last one export; the page says so when an id has rolled over. |
| `name` | `string` | Display title. Not empty. |
| `start` | `string\|null` | ISO 8601 local datetime, no zone. `null` if unknown. |
| `all_day` | `boolean` | When true, `time` is `null`. |
| `date` | `string\|null` | `YYYY-MM-DD`, local. **The site sorts, groups and range-filters on this as a plain string** — lexicographic order is chronological order, no `Date` objects on the hot path. |
| `time` | `string\|null` | `HH:MM` 24h, local. `null` for all-day or unknown-time events. |
| `venue_id` | `string\|null` | Slug FK into `venues[]`. Null for group-only events (a roaming group with no fixed room) — these land like out-of-band events: no hood, no pin, still listed. |
| `venue` | `string\|null` | Denormalized venue name. Null when `venue_id` doesn't resolve. |
| `venue_kind` | `string\|null` | `bar`, `library`, `park`, `church`, `gallery`, `community_center`, `other`, … Open vocabulary — the site must not switch on an exhaustive list. |
| `lat` | `number\|null` | Denormalized from the venue. Null means **not mappable**; the map skips it, the list does not care. |
| `lng` | `number\|null` | Same. |
| `group_id` | `string\|null` | FK into `groups[]`. |
| `group` | `string\|null` | Denormalized group name. |
| `source_id` | `string\|null` | FK into `sources[]`. Internal; never surfaced on a human page. |
| `category` | `string\|null` | Free text from the source, e.g. `"Music, Theater, Fringe"`. Not a controlled vocabulary — **do not build filters on it.** |
| `tags` | `array\|string\|null` | Source-dependent, often null. Unused by the site. |
| `cost` | `string\|null` | Human cost string as published: `"$20"`, `"Free"`, `"Paid"`, `"$10–$25"`. Display verbatim; never parse it for logic. |
| `cost_min_cents` | `number\|null` | Parsed floor in cents. `0` means free, `null` means no signal in the text. |
| `is_free` | `boolean\|null` | **Tri-state.** `true` = confirmed free, `false` = confirmed paid, `null` = unknown. The Free filter matches `true` only (plus a `cost === "Free"` fallback, below). Most events are `null`; that is expected, not a bug. |
| `series_id` | `string\|null` | FK into `series[]`. Null = one-off. |
| `description` | `string\|null` | Plain text, **truncated server-side to ~400 chars**. HTML and CSS are stripped in the adapter, not here. |
| `image` | `string\|null` | Absolute URL. Unused by the site in v1. |
| `url` | `string\|null` | Canonical event page. The only outbound link the site offers. |
| `discovered_via` | `string\|null` | `flyer`, `scrape`, `api`, … Provenance. Internal. |
| `scores` | `object\|null` | **Null when unscored.** See below. |
| `quality` | `number\|null` | The weighted composite, 0–10, one decimal. **Null when unscored.** Null is not zero — an unscored event sorts last and shows no badge, it does not show `0.0`. |
| `score_category` | `string\|null` | Coarse bucket from the scorer's fixed vocabulary (`music`, `visual_arts`, `nightlife`, `class`, …). **Load-bearing:** the bulletin's category chips fold these into six kinds (`bulletin.js` `CAT_OF`). A value the map doesn't know simply matches no chip — safe, but add new scorer buckets to the map. |
| `description_quality` | `string\|null` | `rich` \| `thin` \| `missing`. Ops signal. |

### `events[].scores`

`null`, or an object keyed by dimension name. **Dimensions may be individually
missing** — the site iterates `weights` and skips absent dims rather than assuming six.

```json
"scores": {
  "social_quality":     { "score": 8, "reason": "People come to talk while they work…" },
  "serendipity":        { "score": 6, "reason": "…" },
  "uniqueness":         { "score": 8, "reason": "…" },
  "community_building": { "score": 3, "reason": "…" },
  "solo_friendliness":  { "score": 5, "reason": "…" },
  "value_for_price":    { "score": 6, "reason": "…" }
}
```

| field | type | notes |
|---|---|---|
| `score` | `number` | Integer 0–10. |
| `reason` | `string` | One or two sentences from the scorer, referencing *this* event. **May be `""`.** |

**`reason` is the product's voice, not debug output.** The detail overlay renders all six
reasons as the main body of the panel. An empty reason renders as nothing, never as a
placeholder.

## `series[]`

A recurring thing you can join. **The whole `series` key may be absent** — read it as
`bundle.series || []`.

| field | type | notes |
|---|---|---|
| `id` | `string` | FK target for `events[].series_id`. |
| `name` | `string` | Series name. May be `""`. |
| `venue_id` | `string\|null` | FK into `venues[]`. |
| `venue` | `string\|null` | Denormalized venue name. |
| `neighborhood` | `string\|null` | Denormalized from the venue. Usually null. |
| `cadence` | `string` | `run` \| `weekly` \| `biweekly` \| `monthly` \| `irregular`. |
| `n_dates` | `number` | Distinct dates observed. |
| `median_gap_days` | `number` | Median gap between dates. |
| `first_date` | `string\|null` | **Full ISO datetime**, e.g. `2026-08-19T19:00:00` — *not* `YYYY-MM-DD`, unlike `events[].date`. Kept in this shape for byte-compatibility with Chicago's export. The site reduces it with `dayOf()` (slice to 10) before any comparison or formatting. |
| `last_date` | `string\|null` | Same — full ISO datetime. |

**Joinable** is defined by the site as `cadence ∈ {weekly, biweekly, monthly}`. `run` (a
limited run) and `irregular` are *not* joinable — a run isn't a habit. The `↻` glyph
means joinable, not merely "recurring".

## `venues[]`

| field | type | notes |
|---|---|---|
| `id` | `string` | Slug. |
| `name` | `string` | |
| `kind` | `string\|null` | Same open vocabulary as `venue_kind`. |
| `address` | `string\|null` | |
| `neighborhood` | `string\|null` | **Unreliable — null for most venues.** The map derives neighborhood geometrically from `hoods.geojson` instead of trusting this field. |
| `lat` | `number\|null` | Null = not mappable. |
| `lng` | `number\|null` | |
| `website` | `string\|null` | |
| `hosts_events` | `string\|null` | `regular` \| `occasional` \| `no` \| `unknown`. Ops. (Corrected 2026-08-16, Job H: this said `yes` \| `no` \| `unknown`, a value the engine has never written — `models.Venue.hosts_events` defaults to `unknown` and the real export carries unknown 387 / regular 139 / occasional 35 / no 5. Open vocabulary in practice; ops-only, no page switches on it.) |
| `status` | `string\|null` | `candidate` \| `confirmed` \| … Ops. |
| `verified_by` | `string\|null` | Ops. |
| `maybe_fake` | `boolean` | Ops. |
| `pipeline` | `string` | `built` \| `planned` \| `broken` \| `none` — the best status among sources that actually produced events here. Ops. |
| `feeds` | `array` | Coverage rows. Ops. |
| `event_count` | `number` | Events in this export at this venue. |

## `sources[]` and `groups[]`

Ops collections. `tables.html` renders both; `sources.html` / `source.html?id=` and
`groups.html` / `group.html?id=` are their human boards.

- **`sources[]`**: `id`, `name`, `kind` (adapter), `has_api`, `website`,
  `scraper_status`, `scraper_ref`, `last_scraped`, `publisher_kind`, `publisher_id`,
  `is_aggregator`, `event_count`.
- **`groups[]`**: `id`, `name`, `kind`, `website`, `description`, `join_url`, `status`,
  `verified_by`, `feeds`, `event_count`. `description` (string|null, one public line:
  what the group is, who it's for) and `join_url` (string|null; the site falls back to
  `website`) added 2026-10-13 for the membership directory — a group with neither
  feed nor events is still listed.

## Neighborhoods

`docs/hoods.geojson` — a `FeatureCollection` of `Polygon`/`MultiPolygon` features
(Chicago ships 98 neighborhoods). Coordinates are GeoJSON order (`[lng, lat]`).
Features may overlap; **first match in `order` wins**, so put the hoods you care
about most first.

A feature may carry a `group` that collapses sparse outer hoods into one display
region. **The display/selection key everywhere is `group || name`**; `name` stays
unique and the fine geometry stays intact, so regrouping is a properties-only edit.

| property | type | notes |
|---|---|---|
| `name` | `string` | Unique feature name, e.g. `"West Loop"`. |
| `group` | `string?` | Optional display region for sparse outer hoods. |
| `order` | `number` | Drives match precedence and display order; a group sorts at its earliest member. |

Features may contain interior rings; the point-in-polygon test XORs across all rings of a
polygon so holes are handled. The spotlight mask uses outer rings only.

## What the site guarantees

The engine may ship a partial export at any time — early runs are partially scored, and
that is a normal state, not an error. The site therefore holds these invariants:

1. **Unscored renders.** `scores: null` / `quality: null` → the listing appears with no
   badge, sorts after every scored event, and its overlay says so plainly. It is never
   hidden and never shown as `0.0`.
2. **Missing collections render.** `series` absent → no `↻` anywhere, the Joinable filter
   yields nothing, no crash.
3. **Missing coordinates render.** `lat`/`lng` null → the event is absent from the map and
   present in the list.
4. **Missing text renders.** Null `description`, `url`, `cost`, `venue`, `time` each drop
   their element rather than printing a placeholder.
5. **Free is inferred conservatively.** `is_free === true`, or `is_free` is null *and*
   `cost` matches `/^\s*free\b/i`. `is_free === false` is never treated as free.
6. **Stale exports degrade quietly.** If nothing falls in the next 7 days, the site widens
   to the next window that has listings — or, if every event is in the past, shows the
   most recent week and says so in one mono line.

## Non-goals for v1

No pagination, no incremental fetch, no server. The whole bundle is downloaded once per
page load. A ten-day export of ~600 events is ~1.8 MB (Philadelphia's, the
first city on this engine), which is comfortable for this shape. If the bundle passes ~8 MB, split by window and revisit this contract.

**Export window.** `export.py` ships events in `[today 00:00, today + --days)`, default 10
days, half-open. Events on the horizon date itself are excluded. The site shows a 7-day
window; the extra bundled days are buffer so a missed daily export still covers the week.
The window is a producer default, not part of the contract — a consumer must never assume
the bundle covers any particular span.
