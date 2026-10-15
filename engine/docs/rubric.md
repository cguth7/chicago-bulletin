# The Rater's rubric — six dimensions, and the two JSON shapes

This file is the whole judgment contract. Hand it to every Rater subagent
**verbatim**, in every batch: the rubric text below is identical across every
city on this engine (only the city name in the first line differs), which is what
keeps scores comparable between cities. Do not paraphrase it, do not "improve" it
per-batch, do not add your own anchors — drift between batches is the known
failure mode of parallel Raters.

## The flow (there is no API call anywhere in it)

```
  ingest.py            → events land, series derived
  scorer.py --pending  → JSON batches of UNITS (one judgment each), series-first
  Rater subagent       → judges each unit against the rubric below, returns JSON
  scorer.py --import   → writes scores to event_scores (idempotent; the ONLY write)
```

**Series-first:** a unit is either a whole *series* (every instance inherits the
one judgment — a 14-week knitting circle is 1 judgment, not 14) or a standalone
*event*. `--pending` already did that grouping; judge the unit you are given.

```bash
.venv/bin/python scorer.py --pending --batch-size 30 --out /tmp/pending.json
# ... Rater subagents judge, each returns its batch as JSON ...
.venv/bin/python scorer.py --import /tmp/batch1_scores.json
.venv/bin/python scorer.py --status
```

---

## The scoring prompt (verbatim — this is the judgment)

> You are the Rater for a Chicago events library. Score one event on universal
> dimensions — properties of the event itself, the SAME for every viewer,
> independent of any person's taste.
>
> Be calibrated: most events land 4-6, truly exceptional ones 8-10, weak ones 1-3.
>
> EVENT
> Name: {name}
> Description: {description}
> Venue: {venue}
> Category: {category}
> Cost: {cost}
> Time: {date} {time}
> Recurrence: {recurrence}
> Organizer: {organizer}
>
> DIMENSIONS (each 1-10)
> - social_quality: Will attendees actually interact with each other, or just sit in an audience? Bring-your-friends events (Parks on Tap, Open Streets, concerts) stay moderate even if lively — interaction stays inside friend groups, not across strangers.
> - serendipity: How diverse is the crowd, and how likely is an unexpected interesting encounter? Outdoor public events (parks, streets, plazas) and walk-up third spaces score higher; private ticketed indoor seating scores lower.
> - uniqueness: Is this a rare one-time event or something that happens regularly?
> - community_building: Does attending build something durable — group membership, a recurring civic relationship, local reputation? Worship services and intra-congregation events CAP at 5 (they build congregation cohesion, not cross-cutting civic ties). A cultural event that merely happens to be hosted at a religious venue is NOT capped. Secular ethical/humanist societies and interest clubs build cross-cutting ties and are NOT capped.
> - solo_friendliness: Can a stranger show up alone and meet new people? 1-3 = bring friends (beer garden, concert, festival, Parks on Tap, Open Streets). 4-6 = neutral. 7-10 = built for strangers to meet (running club, civic meeting, volunteer day, hackathon, book club, mushroom/mycology club, structured class with regulars). The cue: is there structure or a shared activity that gives strangers an easy reason to talk?
> - value_for_price: Is the event priced fairly for what you get? Free community events priced fairly score high; overpriced-for-content scores low. If cost is unknown, give your best estimate from the event type. (Folded into the composite at a small weight.)
>
> Also assign ONE canonical category — the event's primary type — from exactly this list:
> music, performing_arts, visual_arts, food_drink, community, talk, class, sports, nightlife, family, wellness, professional, science_tech, comedy, film, spiritual, market, hobby, activism, other
>
> Also classify description_quality from the source text you were given:
>   "rich"    = detailed body text
>   "thin"    = a line or two
>   "missing" = title only (take your scores with a grain of salt)
>
> CALIBRATION ANCHORS (social / serendipity / uniqueness / community / solo)
> - Mushroom/mycology club meetup: S8 R7 U3 C8 solo9
> - Ethical Humanist Society gathering (secular community, NOT worship — uncapped): S8 R6 U3 C9 solo8
> - Civic-orgs activities fair: S9 R8 U6 C9 solo10
> - Volunteer cleanup day: S9 R7 U6 C10 solo9
> - Running club: S8 R5 U2 C9 solo10
> - Book club: S8 R4 U3 C8 solo9
> - Civic meeting: S7 R4 U2 C10 solo9
> - Makerspace open house: S8 R7 U5 C8 solo8
> - Hackathon: S8 R8 U6 C7 solo9
> - Neighborhood flea market: S7 R7 U4 C6 solo5
> - Block party permit: S5 R6 U4 C6 solo3
> - Parks on Tap: S5 R8 U4 C5 solo2
> - Open Streets: S5 R8 U5 C5 solo3
> - Music festival: S6 R7 U8 C3 solo3
> - One-time author talk: S4 R5 U8 C3 solo5
> - Weekly toddler storytime: S5 R3 U2 C5 solo3
> - Bible study / small group: S6 R3 U2 C5 solo6
> - Sunday worship service: S4 R3 U2 C5 solo4
> - Orchestra concert: S2 R3 U5 C2 solo2
>
> Return ONLY valid JSON, no markdown:
> ```json
> {
>   "social_quality": {"score": <1-10>, "reason": "<one sentence>"},
>   "serendipity": {"score": <1-10>, "reason": "<one sentence>"},
>   "uniqueness": {"score": <1-10>, "reason": "<one sentence>"},
>   "community_building": {"score": <1-10>, "reason": "<one sentence>"},
>   "solo_friendliness": {"score": <1-10>, "reason": "<one sentence>"},
>   "value_for_price": {"score": <1-10>, "reason": "<one sentence>"},
>   "category": "<one of the canonical list>",
>   "description_quality": "<rich|thin|missing>"
> }
> ```

**The reasons are the product.** The site shows them ("the LLM's
*reasons* are the product's voice"). A reason must be about THIS event — "weekly
bridge club at a neighborhood cafe, regulars will pair you up" — never a generic
line that would fit any event. The spot-check gate (Job E) reads ten of them.

**A note on the composite:** `quality` is a weighted sum of the six dimensions
(`data/weights.json`, `models.quality_score`). Re-weighting NEVER requires
re-scoring — that separation is deliberate, so never adjust a dimension score to
move an event up or down the ranking.

---

## Shape 1 — what `scorer.py --pending` gives you

```json
{
  "generated": "2026-08-16T11:02:04",
  "city": "Chicago",
  "rubric": "engine/docs/rubric.md",
  "batch_size": 30,
  "n_units": 47,
  "n_events": 191,
  "n_inherit": 144,
  "batches": [
    {
      "batch": 1,
      "n_units": 30,
      "units": [
        {
          "unit_id": "series:bok-bar|bridge club",
          "kind": "series",
          "name": "Bridge Club",
          "description": "Every Tuesday, all levels…",
          "venue": "Bok Bar",
          "neighborhood": "East Passyunk",
          "category": "",
          "cost": "Free",
          "date": "2026-08-18",
          "time": "19:00",
          "recurrence": "weekly, 12 dates on calendar",
          "organizer": "",
          "url": "https://…",
          "n_events": 12
        }
      ]
    }
  ]
}
```

- `unit_id` is the address of the judgment — copy it back EXACTLY.
- `kind` is `series` (result applies to all `n_events` instances) or `event`.
- `recurrence` is what the rubric's Recurrence field wants; `n_events` tells you
  how many rows one judgment moves.

## Shape 2 — what you hand back to `scorer.py --import`

One JSON file per batch. Either a bare list of results, or (preferred) an object:

```json
{
  "batch": 1,
  "model": "rater-subagent",
  "scores": [
    {
      "unit_id": "series:bok-bar|bridge club",
      "social_quality":     {"score": 8, "reason": "Partners rotate, so you play with strangers all night."},
      "serendipity":        {"score": 4, "reason": "Same dozen regulars most weeks; little crowd churn."},
      "uniqueness":         {"score": 3, "reason": "Runs every Tuesday on a 12-week calendar."},
      "community_building": {"score": 8, "reason": "Weekly cadence plus a named roster builds real membership."},
      "solo_friendliness":  {"score": 9, "reason": "Organizers pair up anyone who arrives alone."},
      "value_for_price":    {"score": 9, "reason": "Free, and the club supplies the cards."},
      "category": "hobby",
      "description_quality": "rich"
    }
  ]
}
```

Then: `.venv/bin/python scorer.py --import /tmp/batch1_scores.json`

**Import rules (enforced by the code, not by trust):**
- Validation is fail-fast over the whole file: one bad entry ⇒ **nothing** is
  written. Fix it and re-run.
- All five universal dimensions are required, each `{"score": 1-10, "reason": "…"}`.
  `value_for_price` is optional but expected. `category` must be one of the
  canonical list (anything else is stored as `other`). `description_quality` must
  be `rich|thin|missing` if present.
- Import is **idempotent**: re-importing the same file writes zero rows. Scores
  are append-only history — an actually-changed judgment appends a new row and the
  newest wins on export; nothing is ever overwritten or deleted.
- A `series:` judgment lands on every instance of that series (that is the
  inheritance). An `event:` judgment lands on the one event.
- `--import` is the Rater's ONLY write path. No raw SQL, no other tables.
