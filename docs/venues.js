/* The Bulletin — the venue directory (venues.html) and the venue
   detail page (venue.html). Loaded after app.js, the shared core;
   everything here comes off window.PB. */
(function () {
  'use strict';

  var PB = window.PB;
  var $ = PB.$, esc = PB.esc, fmtShort = PB.fmtShort, chrono = PB.chrono;
  var hostOf = PB.hostOf, sourceKindText = PB.sourceKindText,
    sourceInitials = PB.sourceInitials, boardFilterRow = PB.boardFilterRow,
    evrowHTML = PB.evrowHTML;

  // loaded state, attached to PB by the core just before init runs
  var DB, venueById, hoodOfVenue;

  function venueKindText(kind) {
    if (!kind || kind === 'other') return 'venue';
    return String(kind).replace(/_/g, ' ');
  }

  // derived hood first — the exported neighborhood field is null for most venues
  function venueHood(v) {
    return hoodOfVenue[v.id] || v.neighborhood || null;
  }

  function weekCountByVenue() {
    var counts = {};
    DB.events.forEach(function (e) {
      if (e.venue_id) counts[e.venue_id] = (counts[e.venue_id] || 0) + 1;
    });
    return counts;
  }

  function venueCardHTML(v, n) {
    var hood = venueHood(v);
    return '<a class="scard" href="venue.html?id=' + encodeURIComponent(v.id) + '">' +
      '<span class="n">' + esc(v.name) + '</span>' +
      '<div class="d">' + esc(venueKindText(v.kind)) + (hood ? ' · ' + esc(hood) : '') + '</div>' +
      '<div class="st">' + (v.pipeline === 'built' ? '<span class="st-ok">● live</span> · ' : '') +
      n + ' event' + (n === 1 ? '' : 's') + ' this week</div></a>';
  }

  /* ── venue board (venues.html) ─────────────────────────────────────── */

  // Every venue is the default (not only venues with events this week) — the sort still puts this week's busy
  // rooms first, and "this week" is a pill.
  var venueQuery = '', venueScope = 'all', venueHoodPick = 'all', venueKindPick = 'all';

  // Both facets have long tails (26 kinds, 25 mapped hoods). Only buckets big
  // enough to be worth the row get a pill; everything else stays reachable
  // through "every venue" and the search box, which reads kind and hood too.
  var HOOD_PILLS = 12, KIND_PILLS = 10;

  function venuesInScope(scope, counts) {
    return (DB.venues || []).filter(function (v) {
      if (scope === 'week') return !!counts[v.id];
      if (scope === 'live') return v.pipeline === 'built';
      return true;
    });
  }

  function topBuckets(list, keyOf, limit) {
    var n = {};
    list.forEach(function (v) {
      var k = keyOf(v);
      if (k) n[k] = (n[k] || 0) + 1;
    });
    return Object.keys(n).sort(function (a, b) {
      return n[b] - n[a] || a.localeCompare(b);
    }).slice(0, limit).map(function (k) { return { val: k, label: k, count: n[k] }; });
  }

  // Hood and kind counts are scoped to the current Showing set: a pill reading
  // 58 while the grid shows 12 would be lying about what clicking it does.
  // Only these two rows re-render, so the search box never loses focus.
  function renderVenueFacets() {
    var host = $('venue-facets');
    if (!host) return;
    var scoped = venuesInScope(venueScope, weekCountByVenue());
    var hoodOpts = [{ val: 'all', label: 'all', count: scoped.length }]
      .concat(topBuckets(scoped, venueHood, HOOD_PILLS));
    var kindOpts = [{ val: 'all', label: 'all', count: scoped.length }]
      .concat(topBuckets(scoped, function (v) { return v.kind || 'other'; }, KIND_PILLS)
        .map(function (o) {
          return { val: o.val, label: venueKindText(o.val), count: o.count };
        }));
    host.innerHTML =
      boardFilterRow('hood', 'Neighborhood', hoodOpts, venueHoodPick) +
      boardFilterRow('kind', 'Kind', kindOpts, venueKindPick);
  }

  function renderVenueFilters() {
    var host = $('venue-filters');
    if (!host) return;
    var counts = weekCountByVenue();
    var all = DB.venues || [];
    var scopeOpts = [
      { val: 'all', label: 'every venue', count: all.length },
      { val: 'week', label: 'this week', count: venuesInScope('week', counts).length },
      { val: 'live', label: 'pipeline built', count: venuesInScope('live', counts).length }
    ];
    host.innerHTML =
      '<div class="frow"><span class="flabel">Search</span>' +
      '<input id="venue-search" type="search" placeholder="Venue, street, kind or neighborhood" ' +
      'autocomplete="off" value="' + esc(venueQuery) + '" aria-label="Search venues"></div>' +
      boardFilterRow('scope', 'Showing', scopeOpts, venueScope) +
      '<div id="venue-facets"></div>';
    renderVenueFacets();
  }

  function renderVenues() {
    var host = $('venue-board');
    if (!host) return;
    var counts = weekCountByVenue();
    var all = DB.venues || [];
    var q = venueQuery.trim().toLowerCase();
    var list = venuesInScope(venueScope, counts).filter(function (v) {
      if (venueHoodPick !== 'all' && venueHood(v) !== venueHoodPick) return false;
      if (venueKindPick !== 'all' && (v.kind || 'other') !== venueKindPick) return false;
      if (!q) return true;
      return [v.name, venueKindText(v.kind), venueHood(v), v.address, v.website]
        .join(' ').toLowerCase().indexOf(q) !== -1;
    }).sort(function (a, b) {
      var an = counts[a.id] || 0, bn = counts[b.id] || 0;
      if (an !== bn) return bn - an;
      var ab = a.pipeline === 'built' ? 1 : 0, bb = b.pipeline === 'built' ? 1 : 0;
      if (ab !== bb) return bb - ab;
      var av = a.status === 'verified' ? 1 : 0, bv = b.status === 'verified' ? 1 : 0;
      if (av !== bv) return bv - av;
      return String(a.name).localeCompare(String(b.name));
    });
    host.innerHTML = list.map(function (v) { return venueCardHTML(v, counts[v.id] || 0); }).join('');
    var empty = $('venue-empty'); if (empty) empty.classList.toggle('show', !list.length);
    var gc = $('venue-count'); if (gc) gc.textContent = list.length + ' / ' + all.length;
    var tally = $('venue-tally'); if (tally) tally.textContent = list.length + ' of ' + all.length + ' venues';
  }

  function stampVenueBoard() {
    var st = $('venue-stamp');
    if (st && DB.generated) st.textContent = 'stamped by export · ' + fmtShort(DB.generated.slice(0, 10));
    var counts = weekCountByVenue();
    var all = DB.venues || [];
    var hoods = {};
    all.forEach(function (v) { var hd = venueHood(v); if (hd) hoods[hd] = 1; });
    function set(id, val) { var el = $(id); if (el) el.textContent = val; }
    set('venue-known', DB.meta && DB.meta.venues != null ? DB.meta.venues : all.length);
    set('venue-live', venuesInScope('live', counts).length);
    set('venue-week', venuesInScope('week', counts).length);
    set('venue-hoods', Object.keys(hoods).length);
  }

  function wireVenueFilters() {
    var host = $('venue-filters');
    if (!host) return;
    host.addEventListener('input', function (ev) {
      if (ev.target.id === 'venue-search') { venueQuery = ev.target.value; renderVenues(); }
    });
    host.addEventListener('click', function (ev) {
      var b = ev.target.closest('.pillbtn');
      if (!b) return;
      var row = b.closest('[data-bfilter]');
      if (!row) return;
      var key = row.dataset.bfilter;
      if (key === 'scope') venueScope = b.dataset.val;
      else if (key === 'hood') venueHoodPick = b.dataset.val;
      else venueKindPick = b.dataset.val;
      row.querySelectorAll('.pillbtn').forEach(function (p) {
        p.setAttribute('aria-pressed', p === b ? 'true' : 'false');
      });
      // a narrower scope can drop the pressed hood or kind out of the data
      // entirely; reset them rather than leave a pill matching nothing
      if (key === 'scope') {
        venueHoodPick = 'all';
        venueKindPick = 'all';
        renderVenueFacets();
      }
      renderVenues();
    });
  }

  /* ── venue detail ──────────────────────────────────────────────────── */

  function venueLede(v, hood) {
    var feeds = v.feeds || [];
    var kind = venueKindText(v.kind);
    var h = (/^[aeiou]/i.test(kind) ? 'An ' : 'A ') + esc(kind) +
      (hood ? ' in ' + esc(hood) : '') +
      (v.address ? ', at ' + esc(String(v.address).replace(/\.\s*$/, '')) : '') + '.';
    if (v.hosts_events === 'regular') h += ' It hosts events regularly.';
    else if (v.hosts_events === 'occasional') h += ' It hosts events now and then.';
    if (v.pipeline === 'built') {
      h += ' ' + (feeds.length > 1
        ? '<code>' + feeds.length + ' pipelines</code> watch its listings'
        : 'A pipeline reads its listings') +
        ', and everything they land is scored like the rest of the bulletin.';
    } else if (feeds.length) {
      h += ' Feeds are linked, but no pipeline has landed events from here yet.';
    } else {
      h += ' The bulletin knows this venue, but no pipeline reads its calendar yet.';
    }
    return h;
  }

  function renderVenueDetail() {
    var host = $('venue-detail');
    if (!host) return;
    var id = new URLSearchParams(location.search).get('id');
    var v = id ? venueById[id] : null;
    if (!v) {
      host.innerHTML = '<p class="note">No venue called <b>' + esc(id || '(none)') +
        '</b> in the ledger. <a href="venues.html">Back to the venue directory.</a></p>';
      return;
    }
    document.title = v.name + ' — ' + ((window.CITY_CONFIG || {}).siteName || 'The Philly Bulletin');
    var crumb = $('crumb-name'); if (crumb) crumb.textContent = v.name;

    var evs = DB.events.filter(function (e) { return e.venue_id === v.id; }).sort(chrono);
    var feeds = v.feeds || [];
    var hood = venueHood(v);
    var built = v.pipeline === 'built';
    var domain = hostOf(v.website);
    var bySrc = {};
    evs.forEach(function (e) {
      if (e.source_id) bySrc[e.source_id] = (bySrc[e.source_id] || 0) + 1;
    });

    var h = '<header class="dhero"><div>' +
      (built ? '<span class="live">Live</span>' : '<span class="live plan">Known</span>') +
      '<div class="titleline"><div class="logo" aria-hidden="true">' + esc(sourceInitials(v.name)) + '</div>' +
      '<h1>' + esc(v.name) + '</h1>' +
      (domain ? '<a class="domainchip" href="' + esc(v.website) + '" target="_blank" rel="noopener">' + esc(domain) + ' ↗</a>' : '') +
      '</div>' +
      '<p class="lede">' + venueLede(v, hood) + '</p>' +
      '<div class="tags">' +
      '<span class="tag">' + esc(venueKindText(v.kind)) + '</span>' +
      (hood ? '<span class="tag plain">' + esc(hood) + '</span>' : '') +
      '<span class="tag plain">' + (built ? 'pipeline built' : 'no pipeline yet') + '</span>' +
      '</div>' +
      '<div class="herostats">' +
      '<div><div class="l">Events this week</div><div class="v">' + evs.length + '</div></div>' +
      '<div><div class="l">Feeds</div><div class="v">' + feeds.length + '</div></div>' +
      (hood ? '<div><div class="l">Neighborhood</div><div class="v">' + esc(hood) + '</div></div>' : '') +
      (v.verified_by ? '<div><div class="l">Verified by</div><div class="v">' + esc(v.verified_by) + '</div></div>' : '') +
      '</div></div>' +
      '<aside class="factcard"><div class="hd"><span class="t">Venue facts</span>' +
      '<span class="r"><b>' + evs.length + '</b> this week</span></div>' +
      (v.address ? '<div class="fact"><span>address</span><span class="v">' + esc(v.address) + '</span></div>' : '') +
      (hood ? '<div class="fact"><span>neighborhood</span><span class="v">' + esc(hood) + '</span></div>' : '') +
      '<div class="fact"><span>kind</span><span class="v">' + esc(venueKindText(v.kind)) + '</span></div>' +
      '<div class="fact"><span>pipeline</span><span class="v">' + esc(v.pipeline || 'none') + '</span></div>' +
      (v.hosts_events && v.hosts_events !== 'unknown'
        ? '<div class="fact"><span>hosts events</span><span class="v">' + esc(v.hosts_events) + '</span></div>' : '') +
      '<div class="fact"><span>feeds</span><span class="v">' + feeds.length + '</span></div>' +
      '<div class="ft"><span>' + (built ? '<b>built</b> — a live probe landed events here'
        : '<b>no pipeline</b> — nothing lands here yet') +
      '</span><span class="sh">re-checked on every export</span></div></aside></header>';

    h += '<div class="dmain"><nav class="toc"><div class="t">On this page</div>' +
      '<a href="#week">This week</a>' +
      (feeds.length ? '<a href="#feeds">Feeds</a>' : '') +
      '<a href="#more">More venues</a></nav><div class="dcontent">';

    if (evs.length) {
      var shown = evs.slice(0, 12);
      h += '<section id="week"><div class="seclabel"><span>What the pipelines landed here</span>' +
        '<span>tap a row for the six reasons</span></div>' +
        '<h2>This week at ' + esc(v.name) + '</h2>' +
        '<div class="week">' + shown.map(evrowHTML).join('') +
        '<div class="ft">' + evs.length + ' event' + (evs.length === 1 ? '' : 's') +
        ' this week at this venue · every row carries time, cost, and its scores</div></div></section>';
    } else {
      h += '<section id="week"><h2>This week at ' + esc(v.name) + '</h2>' +
        '<p class="note empty">Nothing from this venue landed in the current window.</p></section>';
    }

    if (feeds.length) {
      h += '<section id="feeds"><div class="seclabel"><span>' + feeds.length +
        (feeds.length === 1 ? ' pipeline watches' : ' pipelines watch') + ' this venue</span></div>' +
        '<h2>Feeds</h2><div class="cov">';
      feeds.forEach(function (f) {
        var n = bySrc[f.source_id] || 0;
        var glyph = f.scraper_status === 'built'
          ? (n ? '<span class="st-ok">● built</span>' : '<span class="st-warn">▲ built</span>')
          : f.scraper_status === 'planned' ? '<span class="st-plan">○ planned</span>'
            : '<span class="st-plan">○ known</span>';
        h += '<div class="covrow"><div class="covhead"><span class="pill">SOURCE</span>' +
          '<span class="vname"><a href="source.html?id=' + encodeURIComponent(f.source_id) + '">' +
          esc(f.name || f.source_id) + '</a>' +
          ' <span class="hood">· ' + esc(sourceKindText(f.kind)) + (f.primary ? ' · primary' : '') + '</span></span>' +
          '<span class="yield">' + (n ? n + ' this week' : 'no yield this week') + '</span>' +
          '<span class="covnote">' + glyph + '</span></div></div>';
      });
      h += '</div></section>';
    }

    var counts = weekCountByVenue();
    var others = DB.venues.filter(function (o) { return o.id !== v.id && counts[o.id]; })
      .sort(function (a, b) {
        return counts[b.id] - counts[a.id] || String(a.name).localeCompare(String(b.name));
      }).slice(0, 4);
    if (others.length) {
      h += '<section id="more"><div class="seclabel"><span>Also on the bulletin this week</span>' +
        '<span><a href="venues.html">browse all ' + (DB.venues || []).length + ' →</a></span></div>' +
        '<h2>More venues</h2><div class="strip">' +
        others.map(function (o) { return venueCardHTML(o, counts[o.id]); }).join('') +
        '</div></section>';
    }

    h += '</div></div>';
    host.innerHTML = h;
  }

  PB.init = function () {
    DB = PB.DB; venueById = PB.venueById; hoodOfVenue = PB.hoodOfVenue;
    if ($('venue-board')) {
      renderVenueFilters(); renderVenues(); stampVenueBoard(); wireVenueFilters();
    } else {
      renderVenueDetail();
    }
  };
})();
