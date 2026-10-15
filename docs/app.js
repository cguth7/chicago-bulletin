/* The Bulletin — the shared core. Loads data.json per CONTRACT.md
   (contract v1), holds the state and helpers every page needs, and renders
   the detail sheet. Each page also loads its own small surface script
   (bulletin.js, calendar.js, sources.js, venues.js, event.js), which reads
   this file's exports from window.PB and registers PB.init. No framework,
   no build step. Every field is treated as nullable; a partial export is a
   normal state. */
(function () {
  'use strict';

  /* ── constants ─────────────────────────────────────────────────────── */

  // Another city's page directory (docs/cambridge/…) reuses these scripts by
  // defining window.CITY_CONFIG in a small city.js loaded first; every value
  // has a philly default so philly's pages need no config at all.
  var CITY = window.CITY_CONFIG || {};
  var STORE = (CITY.storeNs || 'pb');   // session keys, per city — one origin serves both

  // A limited run is not a habit. ↻ means "you can join this".
  var JOINABLE = { weekly: 1, biweekly: 1, monthly: 1 };

  // Display only — JOINABLE above is the logic. Anything unlisted prints as-is.
  var CADENCE = { weekly: 'Every week', biweekly: 'Every other week', monthly: 'Every month' };

  // Used only if the bundle omits weights; order = display order.
  var DIM_FALLBACK = ['social_quality', 'serendipity', 'solo_friendliness',
    'community_building', 'uniqueness', 'value_for_price'];

  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var MONL = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
    'August', 'September', 'October', 'November', 'December'];

  /* ── state ─────────────────────────────────────────────────────────── */

  var DB = null;              // the bundle
  var HOODS = null;           // geojson FeatureCollection, or null if it failed
  var venueById = {};
  var seriesById = {};
  var sourceById = {};
  var hoodOfVenue = {};       // venue_id -> hood name, derived once at load

  var F = { free: false, solo: false, join: false, minq: 0, win: 7, cat: '',
    day: '', hood: CITY.defaultHood || 'Rittenhouse', hoodChosen: false, query: '', advOpen: false };
  var WIN = { start: '0000-01-01', end: '9999-12-31', label: '', note: '' };

  /* ── small helpers ─────────────────────────────────────────────────── */

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function todayISO() {
    var d = new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }

  // Some bundle fields are plain YYYY-MM-DD (events[].date) and some are full
  // ISO datetimes (generated, series[].first_date/last_date — kept that way for
  // byte-compatibility with Chicago's export). Everything is reduced to a day
  // key here so callers never have to care which they were handed.
  function dayOf(v) {
    return v == null ? null : String(v).slice(0, 10);
  }

  // Dates are compared as YYYY-MM-DD strings everywhere; lexicographic order is
  // chronological order. Date objects only appear where a weekday is needed.
  function dateObj(iso) {
    var p = dayOf(iso).split('-');
    return new Date(+p[0], +p[1] - 1, +p[2]);
  }
  function shiftISO(iso, days) {
    var d = dateObj(iso);
    d.setDate(d.getDate() + days);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function fmtDayHead(iso) {
    var d = dateObj(iso);
    return DOW[d.getDay()] + ' · ' + MON[d.getMonth()] + ' ' + d.getDate();
  }
  function fmtShort(iso) {
    if (!iso) return '';
    var d = dateObj(iso);
    if (isNaN(d.getTime())) return '';
    return MON[d.getMonth()] + ' ' + d.getDate();
  }
  var DOWL = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  function longName(d) {
    return DOWL[d.getDay()] + ', ' + MONL[d.getMonth()] + ' ' + d.getDate() + ', ' + d.getFullYear();
  }
  function fmtTime(e) {
    if (e.all_day) return 'All day';
    if (!e.time) return '';
    var p = e.time.split(':'), h = +p[0], m = p[1] || '00';
    var ap = h >= 12 ? 'PM' : 'AM';
    h = h % 12; if (h === 0) h = 12;
    return h + ':' + m + ' ' + ap;
  }
  function dayChoiceLabel(iso) {
    if (iso === 'week') return 'This week';
    var delta = Math.round((dateObj(iso) - dateObj(todayISO())) / 86400000);
    if (delta === 0) return 'Today';
    if (delta === 1) return 'Tomorrow';
    var d = dateObj(iso);
    return DOWL[d.getDay()] + ' · ' + MON[d.getMonth()] + ' ' + d.getDate();
  }

  /* ── contract-level accessors (all null-safe) ──────────────────────── */

  function seriesOf(e) {
    return (e && e.series_id && seriesById[e.series_id]) || null;
  }
  function isJoinable(e) {
    var s = seriesOf(e);
    return !!(s && JOINABLE[s.cadence]);
  }
  // is_free is tri-state; null means unknown, not free. Fall back to the
  // published cost string only when the boolean has no opinion.
  function isFree(e) {
    if (e.is_free === true) return true;
    if (e.is_free === false) return false;
    return typeof e.cost === 'string' && /^\s*free\b/i.test(e.cost);
  }
  function costLabel(e) {
    if (e.cost) return e.cost;
    if (e.is_free === true) return 'Free';
    return '';
  }
  function dimScore(e, dim) {
    var s = e.scores && e.scores[dim];
    return s && typeof s.score === 'number' ? s.score : null;
  }
  function hoodOf(e) {
    return (e.venue_id && hoodOfVenue[e.venue_id]) || null;
  }

  /* ── point in polygon (handles interior rings) ─────────────────────── */

  function inRings(lat, lng, rings) {
    var inside = false;
    for (var r = 0; r < rings.length; r++) {
      var ring = rings[r];
      for (var i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        var xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
        if ((yi > lat) !== (yj > lat) &&
            lng < (xj - xi) * (lat - yi) / (yj - yi) + xi) inside = !inside;
      }
    }
    return inside;
  }
  function inFeature(lat, lng, f) {
    var bb = f._bb;
    if (bb && (lat < bb[0] || lat > bb[2] || lng < bb[1] || lng > bb[3])) return false;
    var g = f.geometry;
    if (!g) return false;
    if (g.type === 'Polygon') return inRings(lat, lng, g.coordinates);
    if (g.type === 'MultiPolygon') {
      for (var i = 0; i < g.coordinates.length; i++) {
        if (inRings(lat, lng, g.coordinates[i])) return true;
      }
    }
    return false;
  }
  function bboxOf(f) {
    var lo = [90, 180], hi = [-90, -180];
    var polys = f.geometry.type === 'Polygon' ? [f.geometry.coordinates] : f.geometry.coordinates;
    polys.forEach(function (p) {
      p.forEach(function (ring) {
        ring.forEach(function (c) {
          if (c[1] < lo[0]) lo[0] = c[1]; if (c[1] > hi[0]) hi[0] = c[1];
          if (c[0] < lo[1]) lo[1] = c[0]; if (c[0] > hi[1]) hi[1] = c[0];
        });
      });
    });
    return [lo[0], lo[1], hi[0], hi[1]];
  }

  // One pass at load: every venue gets its neighborhood. The map never
  // re-tests geometry after this.
  function deriveHoods() {
    if (!HOODS) return;
    HOODS.features.forEach(function (f) { f._bb = bboxOf(f); });
    DB.venues.forEach(function (v) {
      if (v.lat == null || v.lng == null) return;
      for (var i = 0; i < HOODS.features.length; i++) {
        if (inFeature(v.lat, v.lng, HOODS.features[i])) {
          // group collapses sparse outer hoods into one display region
          hoodOfVenue[v.id] = HOODS.features[i].properties.group ||
            HOODS.features[i].properties.name;
          return;
        }
      }
    });
  }

  /* ── the window (default: next 7 days, degrading quietly) ──────────── */

  function resolveWindow() {
    var dates = [];
    DB.events.forEach(function (e) { if (e.date) dates.push(e.date); });
    dates.sort();
    if (!dates.length) {
      WIN = { start: '0000-01-01', end: '9999-12-31', label: 'No listings', note: '' };
      return;
    }
    var first = dates[0], last = dates[dates.length - 1], today = todayISO();
    var hasFuture = last >= today;

    if (!F.win) {                                    // "all"
      WIN = hasFuture
        ? { start: today, end: last, label: 'Everything upcoming', note: '' }
        : { start: first, end: last, label: 'The archive',
            note: 'Every listing here has already happened. Nothing new has come in since ' +
                  fmtShort(last) + '.' };
      return;
    }

    var span = F.win, start = today, end = shiftISO(today, span - 1);
    var hit = dates.some(function (d) { return d >= start && d <= end; });
    if (hit) {
      WIN = { start: start, end: end, label: 'Next ' + span + ' days', note: '' };
      return;
    }
    var nextUp = null;
    for (var i = 0; i < dates.length; i++) { if (dates[i] >= today) { nextUp = dates[i]; break; } }
    if (nextUp) {
      WIN = { start: nextUp, end: shiftISO(nextUp, span - 1),
        label: fmtShort(nextUp) + ' – ' + fmtShort(shiftISO(nextUp, span - 1)),
        note: 'Nothing falls in the next ' + span + ' days. This is the next stretch that ' +
              'has something, starting ' + fmtShort(nextUp) + '.' };
    } else {
      WIN = { start: shiftISO(last, -(span - 1)), end: last,
        label: 'The last ' + span + ' days on record',
        note: 'Every listing here has already happened. This is the most recent stretch ' +
              'on record — nothing new has come in since ' + fmtShort(last) + '.' };
    }
  }

  /* ── ordering, shared by every surface ─────────────────────────────── */

  // Ranked: best first, unscored last, then earliest, then alphabetical.
  function rank(a, b) {
    var qa = typeof a.quality === 'number' ? a.quality : -1;
    var qb = typeof b.quality === 'number' ? b.quality : -1;
    if (qa !== qb) return qb - qa;
    var ta = a.time || '99:99', tb = b.time || '99:99';
    if (ta !== tb) return ta < tb ? -1 : 1;
    return String(a.name).localeCompare(String(b.name));
  }
  function chrono(a, b) {
    var da = (a.date || '9999') + (a.time || '99:99');
    var db = (b.date || '9999') + (b.time || '99:99');
    return da < db ? -1 : da > db ? 1 : 0;
  }

  /* ── filter state, kept for the session ────────────────────────────── */

  function saveFilters() {
    try { sessionStorage.setItem(STORE + '.f2', JSON.stringify(F)); } catch (err) { /* private mode */ }
  }
  function loadFilters() {
    try {
      var raw = sessionStorage.getItem(STORE + '.f2');
      if (raw) {
        var o = JSON.parse(raw);
        Object.keys(F).forEach(function (k) { if (k in o) F[k] = o[k]; });
      }
    } catch (err) { /* ignore */ }
  }

  /* ── shared links ──────────────────────────────────────────────────── */

  function venueLink(e) {
    if (!e.venue) return '';
    return e.venue_id && venueById[e.venue_id]
      ? '<a class="vlink" href="venue.html?id=' + encodeURIComponent(e.venue_id) + '">' + esc(e.venue) + '</a>'
      : esc(e.venue);
  }
  function sourceLink(e) {
    var s = e.source_id && sourceById[e.source_id];
    if (!s) return '';
    return '<a class="vlink" href="source.html?id=' + encodeURIComponent(s.id) + '">' + esc(s.name) + '</a>';
  }

  /* ── detail overlay: the reasons are the point ─────────────────────── */

  function orderedDims() {
    var w = DB.weights;
    if (!w) return DIM_FALLBACK;
    var ks = Object.keys(w);
    if (!ks.length) return DIM_FALLBACK;
    return ks.sort(function (a, b) { return w[b] - w[a]; });
  }

  var lastFocus = null;

  // one dimension list, shared by the sheet and the event permalink page
  function dimsHTML(e) {
    var h = '';
    orderedDims().forEach(function (d) {
      var s = e.scores[d];
      if (!s || typeof s.score !== 'number') return;
      h += '<div class="dim">' +
        '<div class="dim-top"><span class="ml">' + esc(d.replace(/_/g, ' ')) + '</span>' +
        '<span class="v">' + s.score + '/10</span></div>' +
        '<div class="dim-track"><div class="dim-fill" style="width:' +
        Math.max(0, Math.min(100, s.score * 10)) + '%"></div></div>' +
        (s.reason ? '<p class="dim-reason">' + esc(s.reason) + '</p>' : '') +
        '</div>';
    });
    return h;
  }

  function openSheet(id) {
    var e = null;
    for (var i = 0; i < DB.events.length; i++) { if (DB.events[i].id === id) { e = DB.events[i]; break; } }
    if (!e) return;
    var body = $('sheet-body');
    if (!body) return;

    var sr = seriesOf(e);
    var when = [];
    if (e.date) when.push('<span>' + esc(longName(dateObj(e.date))) + '</span>');
    var t = fmtTime(e);
    if (t) when.push('<span>' + esc(t) + '</span>');
    if (sr && JOINABLE[sr.cadence]) {
      var last = dayOf(sr.last_date);
      when.push('<span class="cad">↻ ' + esc(CADENCE[sr.cadence] || sr.cadence) +
        (sr.n_dates ? ' · ' + sr.n_dates + ' dates' : '') +
        (last ? ' · through ' + esc(fmtShort(last)) : '') + '</span>');
    }

    var where = [];
    if (e.venue) where.push(venueLink(e));
    var hd = hoodOf(e);
    if (hd) where.push(esc(hd));
    var c = costLabel(e);
    if (c) where.push(esc(c));
    // the outbound link rides the where line, right under the name — the
    // reasons below are long, and reaching the listing shouldn't need a scroll
    if (e.url) {
      where.push('<a class="sheet-out" href="' + esc(e.url) +
        '" target="_blank" rel="noopener">Go to the listing ↗</a>');
    }

    var h = '<div class="sheet-when ml">' + when.join('') + '</div>' +
      '<h2>' + esc(e.name) + '</h2>' +
      (where.length ? '<div class="sheet-where">' + where.join('<span class="dot">·</span>') + '</div>' : '');

    if (e.description) h += '<p class="sheet-desc">' + esc(e.description) + '</p>';

    if (e.scores) {
      h += '<div class="why-head"><span class="ml ml-ink">Why it scores</span>' +
        (typeof e.quality === 'number' ? '<span class="q">' + e.quality.toFixed(1) + ' / 10</span>' : '') +
        '</div>' + dimsHTML(e);
    } else {
      h += '<p class="unscored">Not scored yet. This one has not been read against the ' +
        'six questions — it still shows up, it just has no score.</p>';
    }

    h += '<a class="sheet-perma" href="event.html?id=' + e.id + '">Open as page →</a>';
    var sl = sourceLink(e);
    if (sl) h += '<div class="sheet-src">Found via ' + sl + '</div>';

    body.innerHTML = h;
    lastFocus = document.activeElement;
    $('scrim').classList.add('open');
    $('sheet').classList.add('open');
    $('sheet').scrollTop = 0;
    document.body.style.overflow = 'hidden';
    var cl = $('sheet-close'); if (cl) cl.focus();
  }

  function closeSheet() {
    var s = $('sheet');
    if (!s || !s.classList.contains('open')) return;
    s.classList.remove('open');
    $('scrim').classList.remove('open');
    if (!document.body.classList.contains('mappage')) document.body.style.overflow = '';
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  /* ── shared directory helpers (source/venue boards and details) ────── */

  function sourceKindText(kind) {
    if (!kind) return 'public calendar';
    return String(kind).replace(/_/g, ' ').replace(/\bwp\b/i, 'WordPress');
  }

  function hostOf(url) {
    if (!url) return '';
    var m = String(url).match(/^https?:\/\/(?:www\.)?([^/]+)/i);
    return m ? m[1] : '';
  }

  function sourceInitials(name) {
    var words = String(name).split(/\s+/).filter(Boolean);
    return words.length > 1
      ? (words[0][0] + words[1][0]).toUpperCase()
      : String(name).slice(0, 2).replace(/^\w/, function (c) { return c.toUpperCase(); });
  }

  // shared by the source board and the venue board
  function boardFilterRow(key, label, options, current) {
    var h = '<div class="frow" data-bfilter="' + key + '"><span class="flabel">' + label + '</span>';
    options.forEach(function (o) {
      h += '<button class="pillbtn" type="button" data-val="' + esc(o.val) + '" aria-pressed="' +
        (current === o.val ? 'true' : 'false') + '">' + esc(o.label) +
        '<span class="n">' + o.count + '</span></button>';
    });
    return h + '</div>';
  }

  function evrowHTML(e) {
    return '<div class="evrow" data-ev="' + e.id + '" role="button" tabindex="0">' +
      '<span class="when">' + esc(fmtShort(e.date)) + ' · ' + esc(fmtTime(e)) + '</span>' +
      '<span class="name">' + esc(e.name) +
      ' <span class="at">— ' + esc(e.venue || 'Venue TBA') + (hoodOf(e) ? ' · ' + esc(hoodOf(e)) : '') + '</span></span>' +
      '<span>' + (isFree(e) ? '<span class="free">free</span>' : '') + '</span>' +
      '<span class="q">' + (typeof e.quality === 'number' ? '<b>' + e.quality.toFixed(1) + '</b> quality' : '—') + '</span></div>';
  }

  /* ── the seam to the page scripts ──────────────────────────────────── */

  // Each page loads exactly one surface script after this file. That script
  // reads these helpers, registers PB.init, and gets the loaded state
  // (PB.DB, PB.F, PB.WIN, …) attached just before init is called.
  var PB = window.PB = {
    hooks: {},
    $: $, esc: esc, MON: MON, DOW: DOW,
    todayISO: todayISO, dateObj: dateObj, shiftISO: shiftISO,
    fmtDayHead: fmtDayHead, fmtShort: fmtShort, longName: longName,
    fmtTime: fmtTime, dayChoiceLabel: dayChoiceLabel,
    JOINABLE: JOINABLE, CADENCE: CADENCE,
    seriesOf: seriesOf, isJoinable: isJoinable, isFree: isFree,
    costLabel: costLabel, dimScore: dimScore, hoodOf: hoodOf,
    rank: rank, chrono: chrono, saveFilters: saveFilters,
    venueLink: venueLink, dimsHTML: dimsHTML,
    hostOf: hostOf, sourceKindText: sourceKindText, sourceInitials: sourceInitials,
    boardFilterRow: boardFilterRow, evrowHTML: evrowHTML
  };

  /* ── boot ──────────────────────────────────────────────────────────── */

  // The reader gets a plain sentence; the technical detail goes to the console,
  // where the only person who needs it will be looking.
  function fail(msg) {
    if (window.console && console.error) console.error((CITY.siteName || 'The Philly Bulletin') + ': ' + msg);
    var host = $('bulletin') || $('map') || $('calendar') || $('source-board') ||
      $('source-detail') || $('venue-board') || $('venue-detail') || $('event-detail');
    if (host) {
      host.innerHTML = '<p class="note">The listings did not load. Refresh the page, ' +
        'or try again in a minute.</p>';
    }
  }

  // The dateline is composed here rather than in markup so it never flashes
  // half-empty separators while data.json is in flight.
  function stamp() {
    var dl = $('dateline');
    if (dl) {
      var parts = [];
      if (dl.dataset.today === '1') parts.push(longName(new Date()));
      if (dl.dataset.lead) parts.push(dl.dataset.lead);
      parts.push(DB.events.length + (DB.events.length === 1 ? ' listing' : ' listings'));
      if (DB.generated) parts.push('Updated ' + fmtShort(DB.generated.slice(0, 10)));
      dl.textContent = parts.join('  ·  ');
    }

    var count = DB.events.length;
    var nav = $('nav-count');
    if (nav) nav.textContent = count + (count === 1 ? ' listing this week' : ' listings this week');
    var su = $('stat-updated'); if (su) su.textContent = DB.generated ? fmtShort(DB.generated) : '—';
    var sk = $('source-known'); if (sk) sk.textContent = DB.meta && DB.meta.sources != null ? DB.meta.sources : (DB.sources || []).length;
    var sb = $('source-built'); if (sb) sb.textContent = DB.meta && DB.meta.scrapers_built != null ? DB.meta.scrapers_built : '—';
    var sp = $('source-todo'); if (sp) sp.textContent = DB.meta && DB.meta.scrapers_todo != null ? DB.meta.scrapers_todo : '—';
    var sf = $('source-links'); if (sf) sf.textContent = DB.meta && DB.meta.feed_links != null ? DB.meta.feed_links : '—';
  }

  function boot() {
    // about.html: the "For AI assistants" prompt, copyable in one tap.
    var cp = $('copy-prompt');
    if (cp) {
      cp.addEventListener('click', function () {
        var src = $('ai-prompt');
        if (!src || !navigator.clipboard) return;
        navigator.clipboard.writeText(src.textContent.trim()).then(function () {
          cp.textContent = 'Copied ✓';
          setTimeout(function () { cp.textContent = 'Copy'; }, 1600);
        });
      });
    }

    // A page with no surface script (about.html) needs no 2 MB bundle.
    if (!PB.init) return;

    Promise.all([
      // Default HTTP caching: GitHub Pages serves a 10-minute max-age with an
      // ETag, so a refresh revalidates instead of re-pulling the whole bundle.
      fetch('data.json').then(function (r) {
        if (!r.ok) throw new Error('data.json ' + r.status);
        return r.json();
      }),
      fetch(CITY.hoodsUrl || 'philly_hoods.geojson')
        .then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; })
    ]).then(function (res) {
      DB = res[0];
      HOODS = res[1];
      DB.events = DB.events || [];
      DB.venues = DB.venues || [];
      DB.series = DB.series || [];          // absent in pre-v1 exports
      DB.venues.forEach(function (v) { venueById[v.id] = v; });
      DB.series.forEach(function (s) { seriesById[s.id] = s; });
      (DB.sources || []).forEach(function (s) { sourceById[s.id] = s; });
      deriveHoods();

      loadFilters();
      // The public window is one week even though the bundle carries ~10 days
      // of buffer. Ignore older session state from the former 14/90-day UI so
      // the label and filters stay honest.
      F.win = 7;
      resolveWindow();
      var availableDays = {};
      DB.events.forEach(function (e) {
        if (e.date && e.date >= WIN.start && e.date <= WIN.end) availableDays[e.date] = 1;
      });
      if (F.day !== 'week' && (!F.day || !availableDays[F.day])) {
        F.day = availableDays[todayISO()] ? todayISO() : Object.keys(availableDays).sort()[0] || '';
      }
      var availableHoods = {};
      DB.events.forEach(function (e) { var h = hoodOf(e); if (h) availableHoods[h] = 1; });
      // Older saved state used the whole city as an implicit default. Treat it
      // as the city's default hood until someone deliberately chooses the
      // whole city (philly: Rittenhouse).
      var dz = CITY.defaultHood || 'Rittenhouse';
      if (F.hood === 'all' && !F.hoodChosen && availableHoods[dz]) F.hood = dz;
      if (F.hood !== 'all' && !availableHoods[F.hood]) F.hood = 'all';
      stamp();

      PB.DB = DB; PB.HOODS = HOODS; PB.F = F; PB.WIN = WIN;
      PB.venueById = venueById; PB.seriesById = seriesById;
      PB.sourceById = sourceById; PB.hoodOfVenue = hoodOfVenue;
      PB.init();
    }).catch(function (err) {
      fail(err && err.message ? err.message : String(err));
    });

    // one delegated handler covers listings, panel rows and map popups
    document.addEventListener('click', function (ev) {
      // links nested inside clickable rows (venue, source) navigate; the row
      // action must not swallow them
      if (ev.target.closest('a')) return;
      var t = ev.target.closest('[data-ev]');
      if (t) {
        // the map closes its popup first, via the hook bulletin.js registers
        if (PB.hooks.closePopup) PB.hooks.closePopup();
        openSheet(+t.dataset.ev);
      }
    });

    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { closeSheet(); return; }
      if (ev.key === 'Enter' || ev.key === ' ') {
        if (ev.target && ev.target.closest && ev.target.closest('a')) return;
        var t = ev.target && ev.target.closest && ev.target.closest('[data-ev]');
        if (t) { ev.preventDefault(); openSheet(+t.dataset.ev); }
      }
    });

    var scrim = $('scrim'); if (scrim) scrim.addEventListener('click', closeSheet);
    var cl = $('sheet-close'); if (cl) cl.addEventListener('click', closeSheet);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
