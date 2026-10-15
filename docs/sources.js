/* The Bulletin — the source board (sources.html) and the source
   detail page (source.html). Loaded after app.js, the shared core;
   everything here comes off window.PB. */
(function () {
  'use strict';

  var PB = window.PB;
  var $ = PB.$, esc = PB.esc, fmtShort = PB.fmtShort, chrono = PB.chrono;
  var hostOf = PB.hostOf, sourceKindText = PB.sourceKindText,
    sourceInitials = PB.sourceInitials, boardFilterRow = PB.boardFilterRow,
    evrowHTML = PB.evrowHTML;

  // loaded state, attached to PB by the core just before init runs
  var DB, venueById;

  /* ── source board (current facts only) ─────────────────────────────── */

  var sourceQuery = '', sourceStatus = 'all', sourceAdapter = 'all';

  // one-off adapter kinds collapse to "custom" so the filter row stays short
  function adapterFamily(s, counts) {
    return counts[s.kind] >= 2 ? s.kind : 'custom';
  }

  function adapterCounts() {
    var counts = {};
    (DB.sources || []).forEach(function (s) { counts[s.kind] = (counts[s.kind] || 0) + 1; });
    return counts;
  }

  var qualityBySource = null;   // source_id -> mean event quality, derived once
  function sourceAvgQuality(id) {
    if (!qualityBySource) {
      qualityBySource = {};
      var acc = {};
      (DB.events || []).forEach(function (e) {
        if (typeof e.quality !== 'number' || !e.source_id) return;
        var a = acc[e.source_id] || (acc[e.source_id] = { sum: 0, n: 0 });
        a.sum += e.quality; a.n++;
      });
      Object.keys(acc).forEach(function (k) { qualityBySource[k] = acc[k].sum / acc[k].n; });
    }
    return qualityBySource[id] != null ? qualityBySource[id] : null;
  }

  // 3/10 and below reads red, 7/10 and up reads green, ramp between.
  function qualityInk(q) {
    var t = Math.max(0, Math.min(1, (q - 3) / 4));
    return 'hsl(' + Math.round(t * 130) + ' 62% 34%)';
  }

  function sourceGlyph(s) {
    if (s.scraper_status === 'built') {
      var q = sourceAvgQuality(s.id);
      var avg = typeof q === 'number'
        ? ' · <span class="st-q" style="color:' + qualityInk(q) + '">' + q.toFixed(1) + ' avg</span>'
        : '';
      return (s.event_count || 0) > 0
        ? '<span class="st-ok">● built</span> · ' + s.event_count + ' event' + (s.event_count === 1 ? '' : 's') + ' this week' + avg
        : '<span class="st-warn">▲ built</span> · 0 in window';
    }
    if (s.scraper_status === 'planned') return '<span class="st-plan">○ planned</span> · no pipeline yet';
    return '<span class="st-plan">○ known</span> · not planned yet';
  }

  function sourceCardHTML(s) {
    var domain = hostOf(s.website);
    return '<a class="scard" href="source.html?id=' + encodeURIComponent(s.id) + '">' +
      '<span class="n">' + esc(s.name) + '</span>' +
      '<div class="d">' + esc(domain ? domain + ' · ' : '') + esc(sourceKindText(s.kind)) + '</div>' +
      '<div class="st">' + sourceGlyph(s) + '</div></a>';
  }

  function renderSourceFilters() {
    var host = $('source-filters');
    if (!host) return;
    var all = DB.sources || [];
    var counts = adapterCounts();
    var statusOpts = [{ val: 'all', label: 'all', count: all.length }];
    [['built', 'built'], ['planned', 'planned'], ['none', 'known']].forEach(function (p) {
      var n = all.filter(function (s) { return s.scraper_status === p[0]; }).length;
      if (n) statusOpts.push({ val: p[0], label: p[1], count: n });
    });
    var famCounts = {};
    all.forEach(function (s) {
      var f = adapterFamily(s, counts);
      famCounts[f] = (famCounts[f] || 0) + 1;
    });
    var adapterOpts = [{ val: 'all', label: 'all', count: all.length }];
    Object.keys(famCounts).sort(function (a, b) { return famCounts[b] - famCounts[a]; }).forEach(function (f) {
      adapterOpts.push({ val: f, label: sourceKindText(f), count: famCounts[f] });
    });
    host.innerHTML =
      '<div class="frow"><span class="flabel">Search</span>' +
      '<input id="source-search" type="search" placeholder="Source name or site" autocomplete="off" value="' +
      esc(sourceQuery) + '" aria-label="Search sources"></div>' +
      boardFilterRow('status', 'Status', statusOpts, sourceStatus) +
      boardFilterRow('adapter', 'Adapter', adapterOpts, sourceAdapter);
  }

  function renderSources() {
    var host = $('source-board');
    if (!host) return;
    var counts = adapterCounts();
    var q = sourceQuery.trim().toLowerCase();
    var all = DB.sources || [];
    var sources = all.filter(function (s) {
      if (sourceStatus !== 'all' && s.scraper_status !== sourceStatus) return false;
      if (sourceAdapter !== 'all' && adapterFamily(s, counts) !== sourceAdapter) return false;
      if (!q) return true;
      return [s.name, s.kind, s.website, s.scraper_status].join(' ').toLowerCase().indexOf(q) !== -1;
    }).sort(function (a, b) {
      var ab = a.scraper_status === 'built' ? 1 : 0, bb = b.scraper_status === 'built' ? 1 : 0;
      if (ab !== bb) return bb - ab;
      if ((a.event_count || 0) !== (b.event_count || 0)) return (b.event_count || 0) - (a.event_count || 0);
      return String(a.name).localeCompare(String(b.name));
    });
    host.innerHTML = sources.map(sourceCardHTML).join('');
    var empty = $('board-empty'); if (empty) empty.classList.toggle('show', !sources.length);
    var gc = $('grid-count'); if (gc) gc.textContent = sources.length + ' / ' + all.length;
    var tally = $('source-tally'); if (tally) tally.textContent = sources.length + ' of ' + all.length + ' sources';
  }

  function stampBoard() {
    var st = $('board-stamp');
    if (st && DB.generated) st.textContent = 'stamped by export · ' + fmtShort(DB.generated.slice(0, 10));
    var code = $('board-code');
    if (code) {
      var m = DB.meta || {};
      code.innerHTML = '$ engine export\n' +
        'sources    <span class="g">' + (m.sources || 0) + ' known · ' + (m.scrapers_built || 0) + ' built</span>\n' +
        'events     <span class="g">' + (m.events || 0) + ' in window · ' + (m.scored || 0) + ' scored</span>\n' +
        'venues     <span class="g">' + (m.venues || 0) + ' known · ' + (m.venues_with_feeds || 0) + ' with feeds</span>\n' +
        'window     <span class="g">[today, today + 7d)</span>';
    }
  }

  function wireSourceFilters() {
    var host = $('source-filters');
    if (!host) return;
    host.addEventListener('input', function (ev) {
      if (ev.target.id === 'source-search') { sourceQuery = ev.target.value; renderSources(); }
    });
    host.addEventListener('click', function (ev) {
      var b = ev.target.closest('.pillbtn');
      if (!b) return;
      var row = b.closest('[data-bfilter]');
      if (!row) return;
      if (row.dataset.bfilter === 'status') sourceStatus = b.dataset.val;
      else sourceAdapter = b.dataset.val;
      row.querySelectorAll('.pillbtn').forEach(function (p) {
        p.setAttribute('aria-pressed', p === b ? 'true' : 'false');
      });
      renderSources();
    });
  }

  /* ── source detail page ────────────────────────────────────────────── */

  // venues linked to this source: through the week's events and through
  // venue feed links (which also carry usefulness + notes from the ledger)
  function sourceCoverage(src, evs) {
    var byVenue = {};
    evs.forEach(function (e) {
      if (!e.venue_id) return;
      if (!byVenue[e.venue_id]) byVenue[e.venue_id] = { id: e.venue_id, name: e.venue, count: 0, feed: null };
      byVenue[e.venue_id].count++;
    });
    (DB.venues || []).forEach(function (v) {
      (v.feeds || []).forEach(function (f) {
        if (f.source_id !== src.id) return;
        if (!byVenue[v.id]) byVenue[v.id] = { id: v.id, name: v.name, count: 0, feed: null };
        byVenue[v.id].feed = f;
      });
    });
    Object.keys(byVenue).forEach(function (id) {
      var v = venueById[id];
      if (v) byVenue[id].hood = v.neighborhood;
    });
    return Object.keys(byVenue).map(function (id) { return byVenue[id]; })
      .sort(function (a, b) { return b.count - a.count || String(a.name).localeCompare(String(b.name)); });
  }

  function sourceLede(src, evs, coverage) {
    var kind = sourceKindText(src.kind);
    if (src.scraper_status !== 'built') {
      return esc(src.name) + ' is in the registry but has no pipeline yet' +
        (src.scraper_status === 'planned' ? ' — it has been scouted and is planned for a build.' :
          ' — it is known, not yet planned.') + ' Nothing on the bulletin comes from here so far.';
    }
    var base = src.is_aggregator
      ? 'A city-wide aggregator: one pipeline fans out to <code>' + coverage.length +
        ' venues</code>, resolving venue and neighborhood per event.'
      : 'A dedicated calendar: this pipeline reads ' + esc(src.name) + '’s own listings.';
    return base + ' The adapter speaks <code>' + esc(kind) + '</code>' +
      (src.has_api ? ' against a real API' : '') +
      ', and every event it lands is normalized into the same shape as every other source.';
  }

  function renderSourceDetail() {
    var host = $('source-detail');
    if (!host) return;
    var id = new URLSearchParams(location.search).get('id');
    var src = (DB.sources || []).filter(function (s) { return s.id === id; })[0];
    if (!src) {
      host.innerHTML = '<p class="note">No source called <b>' + esc(id || '(none)') +
        '</b> in the ledger. <a href="sources.html">Back to the source board.</a></p>';
      return;
    }
    document.title = src.name + ' — Sources — ' + ((window.CITY_CONFIG || {}).siteName || 'The Philly Bulletin');
    var crumb = $('crumb-name'); if (crumb) crumb.textContent = src.name;

    var evs = DB.events.filter(function (e) { return e.source_id === src.id; }).sort(chrono);
    var coverage = sourceCoverage(src, evs);
    var feedLinks = coverage.filter(function (c) { return c.feed; }).length;
    var built = src.scraper_status === 'built';
    var domain = hostOf(src.website);
    var checked = src.last_scraped ? fmtShort(src.last_scraped) : null;

    var h = '<header class="dhero">' +
      '<div>' +
      (built ? '<span class="live">Live</span>' :
        '<span class="live plan">' + (src.scraper_status === 'planned' ? 'Planned' : 'Known') + '</span>') +
      '<div class="titleline"><div class="logo" aria-hidden="true">' + esc(sourceInitials(src.name)) + '</div>' +
      '<h1>' + esc(src.name) + '</h1>' +
      (domain ? '<a class="domainchip" href="' + esc(src.website) + '" target="_blank" rel="noopener">' + esc(domain) + ' ↗</a>' : '') +
      '</div>' +
      '<p class="lede">' + sourceLede(src, evs, coverage) + '</p>' +
      '<div class="tags">' +
      '<span class="tag">' + (src.is_aggregator ? 'aggregator' : 'venue calendar') + '</span>' +
      '<span class="tag plain">' + esc(sourceKindText(src.kind)) + '</span>' +
      (src.has_api ? '<span class="tag plain">api</span>' : '') +
      '</div>' +
      '<div class="herostats">' +
      '<div><div class="l">Events this week</div><div class="v">' + evs.length + '</div></div>' +
      '<div><div class="l">Venues fed</div><div class="v">' + coverage.length + '</div></div>' +
      '<div><div class="l">Last checked</div><div class="v">' + (checked ? esc(checked) : '—') + '</div></div>' +
      '<div><div class="l">Adapter</div><div class="v">' + esc(sourceKindText(src.kind)) + '</div></div>' +
      '</div></div>' +
      '<aside class="factcard"><div class="hd"><span class="t">Source facts</span>' +
      (checked ? '<span class="r">checked <b>' + esc(checked) + '</b></span>' : '<span class="r">not checked yet</span>') + '</div>' +
      '<div class="fact"><span>status</span><span class="v">' + esc(src.scraper_status === 'none' ? 'known' : src.scraper_status) + '</span></div>' +
      '<div class="fact"><span>adapter</span><span class="v">' + esc(sourceKindText(src.kind)) + '</span></div>' +
      '<div class="fact"><span>public API</span><span class="v">' + (src.has_api ? 'yes' : 'no') + '</span></div>' +
      '<div class="fact"><span>events in window</span><span class="v">' + evs.length + '</span></div>' +
      '<div class="fact"><span>venues fed</span><span class="v">' + coverage.length + '</span></div>' +
      '<div class="fact"><span>venue feed links</span><span class="v">' + feedLinks + '</span></div>' +
      '<div class="ft"><span>' + (built ? '<b>built</b> — earned by a live probe' : '<b>not built</b> — no pipeline yet') +
      '</span><span class="sh">re-checked on every export</span></div></aside>' +
      '</header>';

    h += '<div class="dmain"><nav class="toc"><div class="t">On this page</div>' +
      '<a href="#about">About this source</a>' +
      (evs.length ? '<a href="#week">This week</a>' : '') +
      (coverage.length ? '<a href="#coverage">Coverage</a>' : '') +
      '<a href="#flow">How it flows</a>' +
      '<a href="#more">More sources</a></nav><div class="dcontent">';

    h += '<section id="about"><h2>What is this source?</h2><p class="prose-note">' +
      sourceLede(src, evs, coverage) +
      (built ? ' Events it lands are scored on six dimensions; the reasons ride along to the listing sheet.' : '') +
      '</p></section>';

    if (evs.length) {
      var shown = evs.slice(0, 12);
      h += '<section id="week"><div class="seclabel"><span>Live output — what this source landed</span>' +
        '<span>quality scored on six dimensions</span></div><h2>This week from ' + esc(src.name) + '</h2>' +
        '<div class="week">' + shown.map(evrowHTML).join('') +
        '<div class="ft">' + evs.length + ' event' + (evs.length === 1 ? '' : 's') +
        ' this week from this source · every one carries venue, time, cost, and its scores · tap a row for the reasons</div>' +
        '</div></section>';
    } else if (built) {
      h += '<section id="week"><h2>This week from ' + esc(src.name) + '</h2>' +
        '<p class="note empty">The pipeline is built and checked, but nothing from here landed in the current window.</p></section>';
    }

    if (coverage.length) {
      var covShown = coverage.slice(0, 12);
      h += '<section id="coverage"><div class="seclabel"><span>Venues this source feeds · ' + coverage.length +
        ' total</span></div><h2>Coverage</h2><div class="cov">';
      covShown.forEach(function (c) {
        var note = !c.feed ? '' : c.feed.primary ? 'primary feed' :
          (c.feed.usefulness && c.feed.usefulness !== 'unknown') ? c.feed.usefulness + ' usefulness' : '';
        h += '<div class="covrow"><div class="covhead"><span class="pill">VENUE</span>' +
          '<span class="vname"><a href="venue.html?id=' + encodeURIComponent(c.id) + '">' +
          esc(c.name || c.id) + '</a>' +
          (c.hood ? ' <span class="hood">· ' + esc(c.hood) + '</span>' : '') + '</span>' +
          '<span class="yield">' + (c.count ? c.count + ' this week' : 'no yield yet') + '</span>' +
          '<span class="covnote">' + esc(note) + '</span></div></div>';
      });
      if (coverage.length > covShown.length) {
        h += '<div class="ask">+ ' + (coverage.length - covShown.length) +
          ' more venues · a venue joins this list the first time this source lands an event there</div>';
      }
      h += '</div></section>';
    }

    h += '<section id="flow"><div class="band"><div class="cols"><div>' +
      '<div class="seclabel" style="color:var(--green-ink)"><span>Pipeline · how one feed becomes ranked events</span></div>' +
      '<h2>Scraped, resolved, scored</h2>' +
      '<p class="sub">Every source — aggregator or single venue — flows through the same contract, ' +
      'so the city stays coherent no matter where an event came from.</p>' +
      '<ul class="ticks">' +
      '<li>One normalized event shape, shared by every adapter</li>' +
      '<li>Venues resolved against the ledger, never duplicated</li>' +
      '<li>Recurring series detected, so weekly nights don’t drown the board</li>' +
      '<li>Quality scored on six dimensions · reasons kept, not just numbers</li></ul></div>' +
      '<pre class="code">$ engine ingest --source ' + esc(src.id) + '\n' +
      'status     <span class="g">' + esc(src.scraper_status === 'none' ? 'known' : src.scraper_status) + '</span>\n' +
      'checked    <span class="g">' + (checked ? esc(checked) : 'never') + '</span>\n' +
      'events     <span class="g">' + evs.length + ' in window</span>\n' +
      'resolve    <span class="g">' + coverage.length + ' venue' + (coverage.length === 1 ? '' : 's') + '</span></pre>' +
      '</div></div></section>';

    var others = (DB.sources || []).filter(function (s) { return s.id !== src.id; })
      .sort(function (a, b) { return (b.event_count || 0) - (a.event_count || 0); }).slice(0, 4);
    h += '<section id="more"><div class="seclabel"><span>From the source board</span>' +
      '<span><a href="sources.html">browse all ' + (DB.sources || []).length + ' →</a></span></div>' +
      '<h2>More sources</h2><div class="strip">' + others.map(sourceCardHTML).join('') + '</div></section>';

    h += '</div></div>';
    host.innerHTML = h;
  }

  PB.init = function () {
    DB = PB.DB; venueById = PB.venueById;
    if ($('source-board')) {
      renderSourceFilters(); renderSources(); stampBoard(); wireSourceFilters();
    } else {
      renderSourceDetail();
    }
  };
})();
