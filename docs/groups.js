/* The Bulletin — the group directory (groups.html) and the group
   detail page (group.html). Loaded after app.js, the shared core;
   everything here comes off window.PB. Mirrors venues.js: a group is the
   "who runs it" side of a listing, a venue is the "where". */
(function () {
  'use strict';

  var PB = window.PB;
  var $ = PB.$, esc = PB.esc, fmtShort = PB.fmtShort, chrono = PB.chrono;
  var hostOf = PB.hostOf, sourceKindText = PB.sourceKindText,
    sourceInitials = PB.sourceInitials, boardFilterRow = PB.boardFilterRow,
    evrowHTML = PB.evrowHTML;

  // loaded state, attached to PB by the core just before init runs
  var DB, groupById;

  // some kinds are adjectives in the DB ("civic", "community"); the page
  // reads them as nouns
  var KIND_TEXT = { civic: 'civic group', community: 'community group' };
  function groupKindText(kind) {
    if (!kind || kind === 'other') return 'group';
    return KIND_TEXT[kind] || String(kind).replace(/_/g, ' ');
  }

  // The board is a "which should I join" directory: nobody
  // joins a presenter or a promoter, so those sit behind their own pill.
  var NOT_JOINABLE = { presenter: 1, promoter: 1 };
  function isJoinable(g) { return !NOT_JOINABLE[g.kind]; }
  function joinHref(g) { return g.join_url || g.website || null; }

  function isLive(g) {
    return (g.feeds || []).some(function (f) { return f.scraper_status === 'built'; });
  }

  function weekCountByGroup() {
    var counts = {};
    DB.events.forEach(function (e) {
      if (e.group_id) counts[e.group_id] = (counts[e.group_id] || 0) + 1;
    });
    return counts;
  }

  // A div, not an <a>: the card holds two links (its page, and "join →").
  function groupCardHTML(g, n) {
    var domain = hostOf(g.website);
    var join = isJoinable(g) ? joinHref(g) : null;
    return '<div class="scard gcard">' +
      '<a class="n" href="group.html?id=' + encodeURIComponent(g.id) + '">' + esc(g.name) + '</a>' +
      '<div class="d">' + esc(groupKindText(g.kind)) + (domain ? ' · ' + esc(domain) : '') + '</div>' +
      (g.description ? '<p class="gdesc">' + esc(g.description) + '</p>' : '') +
      '<div class="st">' +
      (join ? '<a class="join" href="' + esc(join) + '" target="_blank" rel="noopener">join →</a>' : '') +
      (n ? '<span class="tail">' + (join ? ' · ' : '') + n + ' on this week</span>'
        : isLive(g) && !join ? '<span class="st-ok">● live</span>' : '') +
      '</div></div>';
  }

  /* ── group board (groups.html) ─────────────────────────────────────── */

  // Default: every joinable group, whether or not it has anything on this
  // week (10-13 — the directory, not the week, is the point of this page).
  var groupQuery = '', groupScope = 'join', groupKindPick = 'all';

  function groupsInScope(scope, counts) {
    return (DB.groups || []).filter(function (g) {
      if (scope === 'join') return isJoinable(g);
      if (scope === 'presenters') return !isJoinable(g);
      if (scope === 'week') return !!counts[g.id];
      if (scope === 'live') return isLive(g);
      return true;
    });
  }

  function kindBuckets(list) {
    var n = {};
    list.forEach(function (g) {
      var k = g.kind || 'other';
      n[k] = (n[k] || 0) + 1;
    });
    return Object.keys(n).sort(function (a, b) {
      return n[b] - n[a] || a.localeCompare(b);
    }).map(function (k) { return { val: k, label: groupKindText(k), count: n[k] }; });
  }

  // Kind counts are scoped to the current Showing set so a pill never
  // promises more cards than clicking it shows. Only this row re-renders,
  // so the search box never loses focus.
  function renderGroupFacets() {
    var host = $('group-facets');
    if (!host) return;
    var scoped = groupsInScope(groupScope, weekCountByGroup());
    var kindOpts = [{ val: 'all', label: 'all', count: scoped.length }].concat(kindBuckets(scoped));
    host.innerHTML = boardFilterRow('kind', 'Kind', kindOpts, groupKindPick);
  }

  function renderGroupFilters() {
    var host = $('group-filters');
    if (!host) return;
    var counts = weekCountByGroup();
    var all = DB.groups || [];
    var scopeOpts = [
      { val: 'join', label: 'groups to join', count: groupsInScope('join', counts).length },
      { val: 'week', label: 'this week', count: groupsInScope('week', counts).length },
      { val: 'presenters', label: 'presenters', count: groupsInScope('presenters', counts).length },
      { val: 'all', label: 'everything', count: all.length }
    ];
    host.innerHTML =
      '<div class="frow"><span class="flabel">Search</span>' +
      '<input id="group-search" type="search" placeholder="Group, kind or website" ' +
      'autocomplete="off" value="' + esc(groupQuery) + '" aria-label="Search groups"></div>' +
      boardFilterRow('scope', 'Showing', scopeOpts, groupScope) +
      '<div id="group-facets"></div>';
    renderGroupFacets();
  }

  function renderGroups() {
    var host = $('group-board');
    if (!host) return;
    var counts = weekCountByGroup();
    var all = DB.groups || [];
    var q = groupQuery.trim().toLowerCase();
    var list = groupsInScope(groupScope, counts).filter(function (g) {
      if (groupKindPick !== 'all' && (g.kind || 'other') !== groupKindPick) return false;
      if (!q) return true;
      return [g.name, groupKindText(g.kind), g.website, g.description, g.status]
        .join(' ').toLowerCase().indexOf(q) !== -1;
    }).sort(function (a, b) {
      // joinable first, then described ones (a card with a sentence beats a
      // bare name), then this week's busy ones, then A–Z
      var aj = isJoinable(a) ? 1 : 0, bj = isJoinable(b) ? 1 : 0;
      if (aj !== bj) return bj - aj;
      var ad = a.description ? 1 : 0, bd = b.description ? 1 : 0;
      if (ad !== bd) return bd - ad;
      var an = counts[a.id] || 0, bn = counts[b.id] || 0;
      if (an !== bn) return bn - an;
      return String(a.name).localeCompare(String(b.name));
    });
    host.innerHTML = list.map(function (g) { return groupCardHTML(g, counts[g.id] || 0); }).join('');
    var empty = $('group-empty'); if (empty) empty.classList.toggle('show', !list.length);
    var gc = $('group-count'); if (gc) gc.textContent = list.length + ' / ' + all.length;
    var tally = $('group-tally'); if (tally) tally.textContent = list.length + ' of ' + all.length + ' groups';
  }

  function stampGroupBoard() {
    var st = $('group-stamp');
    if (st && DB.generated) st.textContent = 'stamped by export · ' + fmtShort(DB.generated.slice(0, 10));
    var counts = weekCountByGroup();
    var all = DB.groups || [];
    var verified = all.filter(function (g) { return g.status === 'verified'; }).length;
    function set(id, val) { var el = $(id); if (el) el.textContent = val; }
    set('group-known', DB.meta && DB.meta.groups != null ? DB.meta.groups : all.length);
    set('group-live', groupsInScope('live', counts).length);
    set('group-week', groupsInScope('week', counts).length);
    set('group-join', groupsInScope('join', counts).length);
    set('group-verified', verified);
  }

  function wireGroupFilters() {
    var host = $('group-filters');
    if (!host) return;
    host.addEventListener('input', function (ev) {
      if (ev.target.id === 'group-search') { groupQuery = ev.target.value; renderGroups(); }
    });
    host.addEventListener('click', function (ev) {
      var b = ev.target.closest('.pillbtn');
      if (!b) return;
      var row = b.closest('[data-bfilter]');
      if (!row) return;
      var key = row.dataset.bfilter;
      if (key === 'scope') groupScope = b.dataset.val;
      else groupKindPick = b.dataset.val;
      row.querySelectorAll('.pillbtn').forEach(function (p) {
        p.setAttribute('aria-pressed', p === b ? 'true' : 'false');
      });
      // a narrower scope can drop the pressed kind out of the data entirely
      if (key === 'scope') {
        groupKindPick = 'all';
        renderGroupFacets();
      }
      renderGroups();
    });
  }

  /* ── group detail ──────────────────────────────────────────────────── */

  function groupLede(g, evs) {
    var feeds = g.feeds || [];
    var kind = groupKindText(g.kind);
    var h = (/^[aeiou]/i.test(kind) ? 'An ' : 'A ') + esc(kind) +
      (g.status === 'verified' ? ' the bulletin has vetted.' : ' the bulletin has filed but not yet vetted.');
    if (isLive(g)) {
      h += ' ' + (feeds.length > 1
        ? '<code>' + feeds.length + ' pipelines</code> watch its listings'
        : 'A pipeline reads its listings') +
        ', and everything they land is scored like the rest of the bulletin.';
    } else if (feeds.length) {
      h += ' Feeds are linked, but no pipeline has landed events from this group yet.';
    } else {
      h += ' The bulletin knows this group, but no pipeline reads its calendar yet.';
    }
    if (!evs.length) h += ' Nothing of theirs is on this week.';
    return h;
  }

  function renderGroupDetail() {
    var host = $('group-detail');
    if (!host) return;
    var id = new URLSearchParams(location.search).get('id');
    var g = id ? groupById[id] : null;
    if (!g) {
      host.innerHTML = '<p class="note">No group called <b>' + esc(id || '(none)') +
        '</b> in the ledger. <a href="groups.html">Back to the group directory.</a></p>';
      return;
    }
    document.title = g.name + ' — ' + ((window.CITY_CONFIG || {}).siteName || 'The Philly Bulletin');
    var crumb = $('crumb-name'); if (crumb) crumb.textContent = g.name;

    var evs = DB.events.filter(function (e) { return e.group_id === g.id; }).sort(chrono);
    var feeds = g.feeds || [];
    var live = isLive(g);
    var domain = hostOf(g.website);
    var bySrc = {};
    evs.forEach(function (e) {
      if (e.source_id) bySrc[e.source_id] = (bySrc[e.source_id] || 0) + 1;
    });
    // where this group actually shows up — the venue side of the same events
    var venueCounts = {};
    evs.forEach(function (e) {
      if (e.venue_id) venueCounts[e.venue_id] = (venueCounts[e.venue_id] || 0) + 1;
    });
    var venueIds = Object.keys(venueCounts).sort(function (a, b) { return venueCounts[b] - venueCounts[a]; });

    var h = '<header class="dhero"><div>' +
      (live ? '<span class="live">Live</span>' : '<span class="live plan">Known</span>') +
      '<div class="titleline"><div class="logo" aria-hidden="true">' + esc(sourceInitials(g.name)) + '</div>' +
      '<h1>' + esc(g.name) + '</h1>' +
      (domain ? '<a class="domainchip" href="' + esc(g.website) + '" target="_blank" rel="noopener">' + esc(domain) + ' ↗</a>' : '') +
      '</div>' +
      (g.description ? '<p class="lede gdesc-lede">' + esc(g.description) + '</p>' : '') +
      (isJoinable(g) && joinHref(g) ? '<p><a class="cta join-cta" href="' + esc(joinHref(g)) +
        '" target="_blank" rel="noopener">How to join →</a></p>' : '') +
      '<p class="lede">' + groupLede(g, evs) + '</p>' +
      '<div class="tags">' +
      '<span class="tag">' + esc(groupKindText(g.kind)) + '</span>' +
      '<span class="tag plain">' + esc(g.status || 'candidate') + '</span>' +
      '<span class="tag plain">' + (live ? 'pipeline built' : 'no pipeline yet') + '</span>' +
      '</div>' +
      '<div class="herostats">' +
      '<div><div class="l">Events this week</div><div class="v">' + evs.length + '</div></div>' +
      '<div><div class="l">Feeds</div><div class="v">' + feeds.length + '</div></div>' +
      (venueIds.length ? '<div><div class="l">Venues this week</div><div class="v">' + venueIds.length + '</div></div>' : '') +
      (g.verified_by ? '<div><div class="l">Verified by</div><div class="v">' + esc(g.verified_by) + '</div></div>' : '') +
      '</div></div>' +
      '<aside class="factcard"><div class="hd"><span class="t">Group facts</span>' +
      '<span class="r"><b>' + evs.length + '</b> this week</span></div>' +
      '<div class="fact"><span>kind</span><span class="v">' + esc(groupKindText(g.kind)) + '</span></div>' +
      '<div class="fact"><span>status</span><span class="v">' + esc(g.status || 'candidate') + '</span></div>' +
      (g.website ? '<div class="fact"><span>website</span><span class="v"><a href="' + esc(g.website) +
        '" target="_blank" rel="noopener">' + esc(domain || g.website) + '</a></span></div>' : '') +
      '<div class="fact"><span>pipeline</span><span class="v">' + (live ? 'built' : feeds.length ? 'planned' : 'none') + '</span></div>' +
      '<div class="fact"><span>feeds</span><span class="v">' + feeds.length + '</span></div>' +
      '<div class="ft"><span>' + (live ? '<b>built</b> — a live probe landed events from this group'
        : '<b>no pipeline</b> — nothing lands here yet') +
      '</span><span class="sh">re-checked on every export</span></div></aside></header>';

    h += '<div class="dmain"><nav class="toc"><div class="t">On this page</div>' +
      '<a href="#week">This week</a>' +
      (venueIds.length ? '<a href="#where">Where</a>' : '') +
      (feeds.length ? '<a href="#feeds">Feeds</a>' : '') +
      '<a href="#more">More groups</a></nav><div class="dcontent">';

    if (evs.length) {
      var shown = evs.slice(0, 12);
      h += '<section id="week"><div class="seclabel"><span>What the pipelines landed from this group</span>' +
        '<span>tap a row for the six reasons</span></div>' +
        '<h2>This week from ' + esc(g.name) + '</h2>' +
        '<div class="week">' + shown.map(evrowHTML).join('') +
        '<div class="ft">' + evs.length + ' event' + (evs.length === 1 ? '' : 's') +
        ' this week from this group · every row carries time, cost, and its scores</div></div></section>';
    } else {
      h += '<section id="week"><h2>This week from ' + esc(g.name) + '</h2>' +
        '<p class="note empty">Nothing from this group landed in the current window.</p></section>';
    }

    if (venueIds.length) {
      h += '<section id="where"><div class="seclabel"><span>The rooms this group uses this week</span></div>' +
        '<h2>Where</h2><div class="cov">';
      venueIds.forEach(function (vid) {
        var v = PB.venueById[vid];
        var n = venueCounts[vid];
        var hood = PB.hoodOfVenue[vid] || (v && v.neighborhood) || null;
        h += '<div class="covrow"><div class="covhead"><span class="pill">VENUE</span>' +
          '<span class="vname"><a href="venue.html?id=' + encodeURIComponent(vid) + '">' +
          esc(v ? v.name : vid) + '</a>' + (hood ? ' <span class="hood">· ' + esc(hood) + '</span>' : '') + '</span>' +
          '<span class="yield">' + n + ' this week</span></div></div>';
      });
      h += '</div></section>';
    }

    if (feeds.length) {
      h += '<section id="feeds"><div class="seclabel"><span>' + feeds.length +
        (feeds.length === 1 ? ' pipeline watches' : ' pipelines watch') + ' this group</span></div>' +
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

    var counts = weekCountByGroup();
    var others = (DB.groups || []).filter(function (o) { return o.id !== g.id && counts[o.id]; })
      .sort(function (a, b) {
        return counts[b.id] - counts[a.id] || String(a.name).localeCompare(String(b.name));
      }).slice(0, 4);
    if (others.length) {
      h += '<section id="more"><div class="seclabel"><span>Also on the bulletin this week</span>' +
        '<span><a href="groups.html">browse all ' + (DB.groups || []).length + ' →</a></span></div>' +
        '<h2>More groups</h2><div class="strip">' +
        others.map(function (o) { return groupCardHTML(o, counts[o.id]); }).join('') +
        '</div></section>';
    }

    h += '</div></div>';
    host.innerHTML = h;
  }

  PB.init = function () {
    DB = PB.DB;
    groupById = {};
    (DB.groups || []).forEach(function (g) { groupById[g.id] = g; });
    if ($('group-board')) {
      renderGroupFilters(); renderGroups(); stampGroupBoard(); wireGroupFilters();
    } else {
      renderGroupDetail();
    }
  };
})();
