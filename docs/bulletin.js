/* The Bulletin — the front page (index.html): the ranked list, the
   map, and the finder bar they share. Loaded after app.js, the shared core;
   everything here comes off window.PB. */
(function () {
  'use strict';

  var PB = window.PB;
  var CITY = window.CITY_CONFIG || {};   // philly defaults when absent (see app.js)
  var CITY_LABEL = CITY.cityLabel || 'Philly';   // the "whole city" choice everywhere
  var $ = PB.$, esc = PB.esc, DOW = PB.DOW;
  var dateObj = PB.dateObj, fmtDayHead = PB.fmtDayHead, fmtShort = PB.fmtShort,
    fmtTime = PB.fmtTime, dayChoiceLabel = PB.dayChoiceLabel;
  var isJoinable = PB.isJoinable, isFree = PB.isFree, costLabel = PB.costLabel,
    dimScore = PB.dimScore, hoodOf = PB.hoodOf, rank = PB.rank, chrono = PB.chrono;
  var saveFilters = PB.saveFilters, venueLink = PB.venueLink;

  // loaded state, attached to PB by the core just before init runs
  var DB, HOODS, F, WIN, venueById;

  var zone = 'Rittenhouse';   // focused neighborhood on the map; "all" means Philly
  var VIEW = 'list';          // which surface is up: 'list' | 'map'

  /* ── the one filter both surfaces read ─────────────────────────────── */

  // A page holding only one surface answers for itself; index.html holds
  // both, and VIEW says which one is up.
  function curView() {
    if (!$('map')) return 'list';
    if (!$('bulletin')) return 'map';
    return VIEW;
  }

  // The hero chips fold the scorer's score_category vocabulary into six kinds.
  // Buckets with no chip (family, market, food_drink, spiritual, other) only
  // show when no chip is pressed.
  var CAT_OF = {
    community: 'civics', activism: 'civics', talk: 'civics',
    sports: 'athletics', wellness: 'athletics',
    music: 'music',
    visual_arts: 'arts', performing_arts: 'arts', film: 'arts', comedy: 'arts',
    hobby: 'clubs', class: 'clubs', science_tech: 'clubs', professional: 'clubs',
    nightlife: 'nightlife'
  };

  function filteredEvents() {
    var isList = !!$('bulletin') && curView() === 'list';
    var q = String(F.query || '').trim().toLowerCase();
    return DB.events.filter(function (e) {
      if (!e.date || e.date < WIN.start || e.date > WIN.end) return false;
      // Day and place are shared wayfinding, not page-specific filters. This
      // keeps the map in the same place and moment as the ranked list.
      if (F.day && F.day !== 'week' && e.date !== F.day) return false;
      if (F.hood !== 'all' && hoodOf(e) !== F.hood) return false;
      if (F.cat && CAT_OF[e.score_category] !== F.cat) return false;
      if (isList && q) {
        var hay = [e.name, e.venue, hoodOf(e), e.group, e.category,
          Array.isArray(e.tags) ? e.tags.join(' ') : e.tags].join(' ').toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      if (F.free && !isFree(e)) return false;
      if (F.solo) { var s = dimScore(e, 'solo_friendliness'); if (s == null || s < 7) return false; }
      if (F.join && !isJoinable(e)) return false;
      if (F.minq && !(typeof e.quality === 'number' && e.quality >= F.minq)) return false;
      return true;
    });
  }

  /* ── filter bar (rendered from JS so both surfaces are identical) ──── */

  var FLAGS = [
    { k: 'free', label: 'Free' },
    { k: 'solo', label: 'Solo-friendly' },
    { k: 'join', label: 'Joinable', glyph: '↻' }
  ];
  var MINQ = [[0, 'Any'], [5, '5+'], [6, '6+'], [7, '7+'], [8, '8+']];

  function advCount() {
    return (F.free ? 1 : 0) + (F.solo ? 1 : 0) + (F.join ? 1 : 0) + (F.minq ? 1 : 0);
  }

  function advBadge() {
    var n = advCount();
    return n ? '<span class="advanced-count">' + n + ' on</span>' : '';
  }

  function advControlsHTML() {
    var h = '<div class="advanced-flags">';
    FLAGS.forEach(function (f) {
      h += '<button class="pillbtn toggle" type="button" data-flag="' + f.k + '" aria-pressed="' +
        (F[f.k] ? 'true' : 'false') + '">' +
        (f.glyph ? '<span class="gl">' + f.glyph + '</span>' : '') + esc(f.label) + '</button>';
    });
    h += '</div><div class="advanced-score"><span class="filter-label">Minimum quality</span>' +
      '<span class="seg-set" role="group" aria-label="Minimum score">';
    MINQ.forEach(function (m) {
      h += '<button type="button" data-minq="' + m[0] + '" aria-pressed="' +
        (F.minq === m[0] ? 'true' : 'false') + '">' + esc(m[1]) + '</button>';
    });
    return h + '</span></div>' +
      '<button class="advanced-reset linkish" type="button" data-reset-advanced="1">Reset advanced</button>';
  }

  // The map's filter bar keeps the disclosure; on the list the same controls
  // hang off the board's day header instead (see advHeadHTML).
  function advancedHTML() {
    var active = advCount();
    return '<details class="advanced"' + (active ? ' open' : '') + '><summary>Advanced settings' +
      advBadge() + '</summary><div class="advanced-in">' + advControlsHTML() + '</div></details>';
  }

  function advHeadHTML() {
    return '<button class="advtoggle" type="button" data-adv="1" aria-expanded="' +
      (F.advOpen ? 'true' : 'false') + '">Advanced settings' + advBadge() +
      '<span class="caret" aria-hidden="true">▾</span></button>';
  }

  function advPanelHTML() {
    return F.advOpen ? '<div class="advbar">' + advControlsHTML() + '</div>' : '';
  }

  function listFinderHTML() {
    var dates = {}, hoods = {};
    DB.events.forEach(function (e) {
      // The bundle carries buffer days beyond the window (and, when stale,
      // days behind it); only days the window can actually show are offered.
      if (e.date && e.date >= WIN.start && e.date <= WIN.end) dates[e.date] = 1;
      var hood = hoodOf(e); if (hood) hoods[hood] = (hoods[hood] || 0) + 1;
    });
    var h = '<div class="finder">' +
      '<label class="find-field find-search"><span>Search</span><input id="event-search" type="search" value="' +
      esc(F.query) + '" placeholder="Events or venues" autocomplete="off"></label>' +
      '<label class="find-field"><span>I’m in</span><select id="hood-select"><option value="all">' + esc(CITY_LABEL) + '</option>';
    // Busiest neighborhoods first — the picker doubles as a "where is stuff" list.
    Object.keys(hoods).sort(function (a, b) {
      return hoods[b] - hoods[a] || String(a).localeCompare(String(b));
    }).forEach(function (hood) {
      h += '<option value="' + esc(hood) + '"' + (F.hood === hood ? ' selected' : '') + '>' +
        esc(hood) + '</option>';
    });
    h += '</select></label><label class="find-field"><span>The day is</span><select id="day-select">';
    Object.keys(dates).sort().forEach(function (day) {
      h += '<option value="' + day + '"' + (F.day === day ? ' selected' : '') + '>' +
        esc(dayChoiceLabel(day)) + '</option>';
    });
    h += '<option value="week"' + (F.day === 'week' ? ' selected' : '') + '>This week</option>';
    // Nothing below the finder: the board's day header carries both the count
    // and the advanced settings, so a second grey bar would only repeat them.
    h += '</select></label></div>';
    return h;
  }

  function mapFinderHTML() {
    var dates = {};
    DB.events.forEach(function (e) {
      if (e.date && e.date >= WIN.start && e.date <= WIN.end) dates[e.date] = 1;
    });
    var h = '<div class="finder map-finder">' +
      '<label class="find-field"><span>It is</span><select id="day-select">';
    Object.keys(dates).sort().forEach(function (day) {
      h += '<option value="' + day + '"' + (F.day === day ? ' selected' : '') + '>' +
        esc(dayChoiceLabel(day)) + '</option>';
    });
    h += '<option value="week"' + (F.day === 'week' ? ' selected' : '') + '>This week</option>';
    h += '</select></label>' +
      '<label class="find-field hood-field"><span>I’m in</span>' +
      '<input id="map-hood-search" type="search" value="' +
      esc(F.hood === 'all' ? CITY_LABEL : F.hood) +
      '" placeholder="Search neighborhoods" autocomplete="off" aria-label="Neighborhood"' +
      ' role="combobox" aria-expanded="false" aria-autocomplete="list" aria-controls="hood-suggest">' +
      '<div class="suggest" id="hood-suggest" role="listbox" aria-label="Neighborhoods" hidden></div>' +
      '</label></div>' + advancedHTML() +
      '<div class="filter-tally ml" id="tally"></div>';
    return h;
  }

  function hoodChoices() {
    var names = [CITY_LABEL];
    if (HOODS) {
      // Busiest neighborhoods first, so an empty search suggests the live ones.
      var counts = {};
      DB.events.forEach(function (e) {
        var h = hoodOf(e); if (h) counts[h] = (counts[h] || 0) + 1;
      });
      var seen = {};
      HOODS.features.map(hoodName).filter(function (name) {
        if (seen[name]) return false;
        seen[name] = 1; return true;
      }).sort(function (a, b) {
        return (counts[b] || 0) - (counts[a] || 0) || String(a).localeCompare(String(b));
      }).forEach(function (name) {
        names.push(name);
      });
    }
    return names;
  }

  // The native <datalist> popup can't be styled and renders near-invisible on
  // some platforms, so the neighborhood search draws its own suggestion list.
  function wireHoodSuggest(host) {
    var open = false, cursor = -1, shown = [];

    function input() { return document.getElementById('map-hood-search'); }
    function box() { return document.getElementById('hood-suggest'); }

    function close() {
      var b = box(), inp = input();
      open = false; cursor = -1; shown = [];
      if (b) { b.hidden = true; b.innerHTML = ''; }
      if (inp) inp.setAttribute('aria-expanded', 'false');
    }

    function paint() {
      var b = box();
      if (!b) return;
      b.innerHTML = shown.map(function (name, i) {
        return '<div class="suggest-opt' + (i === cursor ? ' active' : '') +
          '" role="option" id="hood-opt-' + i + '" aria-selected="' + (i === cursor) +
          '" data-suggest="' + esc(name) + '">' + esc(name) + '</div>';
      }).join('');
      b.hidden = !shown.length;
      var inp = input();
      if (inp) {
        inp.setAttribute('aria-expanded', shown.length ? 'true' : 'false');
        if (cursor >= 0) inp.setAttribute('aria-activedescendant', 'hood-opt-' + cursor);
        else inp.removeAttribute('aria-activedescendant');
      }
      open = !!shown.length;
    }

    function show(query) {
      var q = String(query || '').trim().toLowerCase();
      shown = hoodChoices().filter(function (name) {
        return !q || name.toLowerCase().indexOf(q) !== -1;
      });
      cursor = -1;
      paint();
    }

    function choose(name) {
      close();
      var hood = matchHood(name);
      if (hood != null) setZone(hood);
    }

    host.addEventListener('input', function (ev) {
      if (ev.target.id === 'map-hood-search') show(ev.target.value);
    });
    host.addEventListener('focusin', function (ev) {
      if (ev.target.id === 'map-hood-search') { ev.target.select(); show(''); }
    });
    host.addEventListener('keydown', function (ev) {
      if (ev.target.id !== 'map-hood-search') return;
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        if (!open) { show(ev.target.value); return; }
        cursor = ev.key === 'ArrowDown'
          ? (cursor + 1) % shown.length
          : (cursor - 1 + shown.length) % shown.length;
        paint();
      } else if (ev.key === 'Enter') {
        ev.preventDefault();
        if (open && cursor >= 0) choose(shown[cursor]);
        else choose(ev.target.value);
      } else if (ev.key === 'Escape') {
        close();
      }
    });
    // mousedown, not click: the input's blur would close the list first.
    host.addEventListener('mousedown', function (ev) {
      var opt = ev.target.closest('.suggest-opt');
      if (opt) { ev.preventDefault(); choose(opt.dataset.suggest); }
    });
    host.addEventListener('focusout', function (ev) {
      if (ev.target.id !== 'map-hood-search') return;
      setTimeout(function () {
        var inp = input();
        if (inp && document.activeElement !== inp) {
          close();
          inp.value = F.hood === 'all' ? CITY_LABEL : F.hood;
        }
      }, 120);
    });
  }

  // The chips are static markup in both hero rows, so pressed state is
  // painted onto every copy rather than re-rendered.
  function syncCatChips() {
    document.querySelectorAll('.catchips [data-cat]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.dataset.cat === F.cat ? 'true' : 'false');
    });
  }

  // index.html carries two finder hosts, one per surface; only the active
  // view's host holds markup, so control ids stay unique on the page.
  function renderFilters() {
    var listHost = $('filters'), mapHost = $('filters-map');
    var host = mapHost && curView() === 'map' ? mapHost : (listHost || mapHost);
    if (!host) return;
    if (listHost && listHost !== host) listHost.innerHTML = '';
    if (mapHost && mapHost !== host) mapHost.innerHTML = '';
    host.innerHTML = curView() === 'map' ? mapFinderHTML() : listFinderHTML();
  }

  function matchHood(value) {
    var wanted = String(value || '').trim().toLowerCase();
    var allAliases = CITY.allAliases || ['philly', 'all philadelphia', 'philadelphia'];
    if (allAliases.indexOf(wanted) !== -1) return 'all';
    if (!HOODS) return null;
    var names = HOODS.features.map(hoodName);
    for (var i = 0; i < names.length; i++) {
      if (names[i].toLowerCase() === wanted) return names[i];
    }
    var starts = names.filter(function (name) { return name.toLowerCase().indexOf(wanted) === 0; });
    return starts.length === 1 ? starts[0] : null;
  }

  function wireFilters(onChange) {
    ['filters', 'filters-map'].forEach(function (id) {
      var host = $(id);
      if (!host) return;
      host.addEventListener('input', function (ev) {
        if (ev.target.id !== 'event-search') return;
        F.query = ev.target.value;
        saveFilters(); onChange();
      });
      host.addEventListener('change', function (ev) {
        if (ev.target.id === 'hood-select') { F.hood = ev.target.value; F.hoodChosen = true; }
        else if (ev.target.id === 'day-select') F.day = ev.target.value;
        else return;
        saveFilters(); onChange();
      });
      // the map finder's neighborhood search lives in filters-map on
      // index.html, or in the lone filters host on a map-only page
      if ($('map') && (id === 'filters-map' || !$('bulletin'))) wireHoodSuggest(host);
      host.addEventListener('click', function (ev) {
        var b = ev.target.closest('button');
        if (!b) return;
        if (b.dataset.flag) { F[b.dataset.flag] = !F[b.dataset.flag]; }
        else if (b.dataset.minq != null) { F.minq = +b.dataset.minq; }
        else if (b.dataset.resetAdvanced) { F.free = F.solo = F.join = false; F.minq = 0; }
        else return;
        saveFilters();
        renderFilters();
        onChange();
      });
    });
  }

  function setTally(n) {
    var t = $('tally');
    if (t) t.textContent = n + (n === 1 ? ' listing' : ' listings') +
      (F.day ? ' · ' + dayChoiceLabel(F.day) : ' · ' + WIN.label);
  }

  function updateMapCopy(n) {
    var context = $('map-context');
    var place = F.hood === 'all' ? CITY_LABEL : F.hood;
    if (context) context.textContent = (F.day ? dayChoiceLabel(F.day) : WIN.label) + ' · ' + place;
    var nav = $('nav-count');
    if (nav) nav.textContent = n + (n === 1 ? ' listing · ' : ' listings · ') + place;
  }

  /* ── the bulletin ──────────────────────────────────────────────────── */

  function scoreCell(e) {
    return typeof e.quality === 'number'
      ? '<div class="entry-score"><b>' + e.quality.toFixed(1) + '</b><span> quality</span></div>'
      : '<div class="entry-score none" title="Not scored yet">—</div>';
  }

  function metaLine(e) {
    var bits = [];
    if (e.venue) bits.push(venueLink(e));
    var h = hoodOf(e);
    if (h) bits.push(esc(h));
    return bits.join('<span class="dot">·</span>');
  }

  function updateListCopy(evs) {
    var label = F.day ? dayChoiceLabel(F.day) : 'Today';
    var nav = $('nav-count'); if (nav) nav.textContent = evs.length + (evs.length === 1 ? ' listing · ' : ' listings · ') + label.toLowerCase();
  }

  function renderBulletin() {
    var host = $('bulletin');
    if (!host) return;
    var evs = filteredEvents();
    setTally(evs.length);
    updateListCopy(evs);

    var h = '';
    if (WIN.note) h += '<p class="note">' + esc(WIN.note) + '</p>';

    // The advanced settings take the day label's place at the head of the
    // board — the day picker right above already names the day — and the panel
    // drops out of that same row, so the filter bar needs no second grey strip.
    function dayheadHTML(label, n, first) {
      return '<div class="dayhead">' +
        (first ? advHeadHTML() : '<span class="d">' + esc(label) + '</span>') +
        '<span class="ml n">' + n + (n === 1 ? ' listing' : ' listings') + ' · best first</span>' +
        '</div>' + (first ? advPanelHTML() : '');
    }

    if (!evs.length) {
      // the toggle has to survive an empty result, or advanced filters that
      // match nothing leave no way back
      h += '<section class="daygroup">' +
        '<div class="dayhead">' + advHeadHTML() + '</div>' + advPanelHTML() +
        '<p class="note empty">Nothing matches. ' +
        '<button class="linkish" type="button" data-clear="1">Clear the filters</button> ' +
        'or try another neighborhood or day.</p></section>';
      host.innerHTML = h;
      return;
    }

    function entryHTML(e, pos, showDay) {
      var when = showDay ? DOW[dateObj(e.date).getDay()] + (fmtTime(e) ? ' · ' + fmtTime(e) : '') : fmtTime(e);
      return '<article class="entry" role="button" tabindex="0" data-ev="' + e.id + '">' +
        '<div class="entry-rank">' + (pos + 1) + '</div>' +
        '<div class="entry-time">' + esc(when) + '</div>' +
        '<div class="entry-body"><h3 class="entry-name">' + esc(e.name) + '</h3>' +
        (isJoinable(e) ? '<span class="gl-series" title="Happens regularly — you can join it">↻</span>' : '') +
        '<div class="entry-meta">' + metaLine(e) + '</div></div>' +
        '<div class="entry-cost">' + (isFree(e) ? '<span class="free">Free</span>' : esc(costLabel(e))) + '</div>' +
        scoreCell(e) + '</article>';
    }

    if (F.day === 'week') {
      // One flat list for the whole week — the picker already covers per-day
      // views, so here quality outranks chronology.
      var list = evs.slice().sort(function (a, b) {
        var qa = typeof a.quality === 'number' ? a.quality : -1;
        var qb = typeof b.quality === 'number' ? b.quality : -1;
        if (qa !== qb) return qb - qa;
        return chrono(a, b);
      });
      h += '<section class="daygroup">' + dayheadHTML('All week', list.length, true);
      list.forEach(function (e, i) { h += entryHTML(e, i, true); });
      h += '</section>';
    } else {
      var byDay = {};
      evs.forEach(function (e) { (byDay[e.date] = byDay[e.date] || []).push(e); });
      Object.keys(byDay).sort().forEach(function (day, gi) {
        var list = byDay[day].slice().sort(rank);
        h += '<section class="daygroup">' + dayheadHTML(fmtDayHead(day), list.length, gi === 0);
        list.forEach(function (e, i) { h += entryHTML(e, i, false); });
        h += '</section>';
      });
    }
    h += '<div class="board-foot">' + evs.length + (evs.length === 1 ? ' listing' : ' listings') +
      ' · ranked by community-weighted quality · open a row for all six reasons</div>';
    host.innerHTML = h;
  }

  /* ── map ───────────────────────────────────────────────────────────── */

  // generous cover for the spotlight mask, [lat,lng]
  var MASK_OUTER = CITY.maskOuter ||
    [[39.3, -76.3], [39.3, -74.3], [40.7, -74.3], [40.7, -76.3]];

  // The whole-city start frame (philly: the dense center-city strip,
  // University City → Fishtown). Fitting every polygon instead (Chestnut Hill
  // to the far Northeast, Fairmount Park up to East Falls) opens on a view
  // where the strip is a smudge; the outer regions stay reachable by panning
  // or picking.
  var CORE_BOUNDS = CITY.coreBounds || [[39.925, -75.20], [39.975, -75.125]];

  var map = null, hoodLayer = null, maskLayer = null, pinLayer = null, fitZone = null;

  // the shared core closes any open popup before it opens the detail sheet
  PB.hooks.closePopup = function () { if (map) map.closePopup(); };

  // Hairlines have to survive a very light basemap — anything fainter than
  // this simply disappears on Positron.
  // A feature's display name: its region `group` when it has one (sparse
  // outer hoods collapse into "West Philly", "South Philly", ...), else its
  // own name. The selection key everywhere.
  function hoodName(f) {
    return f.properties.group || f.properties.name;
  }

  function hoodStyle(f) {
    var focused = zone !== 'all' && hoodName(f) === zone;
    var hidden = zone !== 'all' && !focused;
    return {
      color: hidden ? 'transparent' : (focused ? '#0c6b2c' : '#a9aaa4'),
      weight: focused ? 2 : 1,
      fill: true, fillColor: focused ? '#0c6b2c' : '#cbe0b8',
      fillOpacity: hidden ? 0 : (focused ? 0.13 : 0.08),
      dashArray: null
    };
  }

  function outerRings(f) {
    var g = f.geometry;
    if (g.type === 'Polygon') return [g.coordinates[0]];
    if (g.type === 'MultiPolygon') return g.coordinates.map(function (p) { return p[0]; });
    return [];
  }
  function flip(ring) { return ring.map(function (c) { return [c[1], c[0]]; }); }

  // A region group spans several features, so a zone maps to a list.
  function featuresFor(name) {
    if (!HOODS) return [];
    return HOODS.features.filter(function (f) { return hoodName(f) === name; });
  }

  function initMap() {
    // zoomSnap 0.25: whole-number snapping put the city one step too far out
    // on a laptop-height window — half the fit was New Jersey.
    map = L.map('map', {
      zoomControl: true, scrollWheelZoom: true, minZoom: 10, zoomSnap: 0.25
    });
    // Layers only attach once the map has a view; without this, the
    // bringToBack/bringToFront calls below touch elements that don't exist
    // yet and boot dies. applyZone's fitBounds reframes it immediately.
    map.setView(CITY.mapCenter || [39.9526, -75.1652], 12);
    // Esri Light Gray (base + labels): keyless. Carto's basemaps started
    // returning "API KEY REQUIRED" tiles in 2026-09.
    var esri = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/';
    L.tileLayer(esri + 'World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
      maxNativeZoom: 16, maxZoom: 19,
      attribution: 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors'
    }).addTo(map);
    L.tileLayer(esri + 'World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}', {
      maxNativeZoom: 16, maxZoom: 19
    }).addTo(map);

    pinLayer = L.layerGroup().addTo(map);

    var key = L.control({ position: 'bottomleft' });
    key.onAdd = function () {
      var el = L.DomUtil.create('div', 'legend');
      el.innerHTML = '<b>Reading the map</b>' +
        'Bigger pin = higher score<br>' +
        'Number = listings at that address<br>' +
        'Dashed = not scored yet';
      L.DomEvent.disableClickPropagation(el);
      return el;
    };
    key.addTo(map);

    if (HOODS) {
      hoodLayer = L.geoJSON(HOODS, {
        style: hoodStyle,
        interactive: false
      }).addTo(map);
      hoodLayer.bringToBack();
    }

    buildPins();
    applyZone(true);
  }

  function setZone(name) {
    zone = name;
    F.hood = name;
    F.hoodChosen = true;
    saveFilters();
    renderFilters();
    if (hoodLayer) hoodLayer.setStyle(hoodStyle);
    buildPins();
    applyZone(true);
  }

  // fit only when the focus actually changed — a filter toggle must never
  // throw away the reader's pan and zoom.
  function applyZone(mayFit) {
    if (!map) return;
    if (maskLayer) { map.removeLayer(maskLayer); maskLayer = null; }
    var feats = zone !== 'all' ? featuresFor(zone) : [];
    if (feats.length) {
      var holes = [];
      feats.forEach(function (f) { holes = holes.concat(outerRings(f).map(flip)); });
      maskLayer = L.polygon([MASK_OUTER].concat(holes), {
        stroke: false, fillColor: '#f7f6f2', fillOpacity: 0.76, interactive: false
      }).addTo(map);
      maskLayer.bringToBack();
      if (hoodLayer) hoodLayer.bringToFront();
    }
    if (mayFit && fitZone !== zone && hoodLayer) {
      var b = L.latLngBounds(feats.length ? [] : CORE_BOUNDS);
      hoodLayer.eachLayer(function (l) {
        if (feats.length && hoodName(l.feature) === zone) b.extend(l.getBounds());
      });
      if (b.isValid()) {
        // 760px matches the stylesheet's mobile block, where the side panel
        // is gone; the generous padding keeps a focused hood zoomed out
        // enough to see its surroundings on a phone.
        var narrow = window.matchMedia('(max-width:760px)').matches;
        map.fitBounds(b, feats.length
          ? (narrow ? { padding: [56, 44] }
            : { paddingTopLeft: [24, 24], paddingBottomRight: [364, 24] })
          : { padding: [22, 22] });
      }
      fitZone = zone;
    }
    renderPanel();
  }

  function buildPins() {
    if (!map) return;
    var evs = filteredEvents();
    setTally(evs.length);
    updateMapCopy(evs.length);

    var byVenue = {};
    evs.forEach(function (e) {
      if (!e.venue_id) return;
      var v = venueById[e.venue_id];
      if (!v || v.lat == null || v.lng == null) return;
      (byVenue[e.venue_id] = byVenue[e.venue_id] || []).push(e);
    });

    // Size ramp normalised against the current filtered spread, so the pins
    // stay legible when quality only spans a narrow band.
    var tops = [];
    Object.keys(byVenue).forEach(function (vid) {
      var qs = byVenue[vid].map(function (e) { return e.quality; })
        .filter(function (q) { return typeof q === 'number'; });
      if (qs.length) tops.push(Math.max.apply(null, qs));
    });
    tops.sort(function (a, b) { return a - b; });
    var lo = tops.length ? tops[Math.floor(tops.length * 0.10)] : 0;
    var hi = tops.length ? tops[Math.floor(tops.length * 0.90)] : 10;
    var norm = function (q) {
      if (hi <= lo) return 0.5;
      return Math.max(0, Math.min(1, (q - lo) / (hi - lo)));
    };

    pinLayer.clearLayers();
    Object.keys(byVenue).forEach(function (vid) {
      var v = venueById[vid], list = byVenue[vid].slice().sort(rank);
      var qs = list.map(function (e) { return e.quality; })
        .filter(function (q) { return typeof q === 'number'; });
      var top = qs.length ? Math.max.apply(null, qs) : null;
      var n = top == null ? 0 : norm(top);
      var d = top == null ? 14 : Math.round(18 + n * 20);
      var fill = top == null
        ? 'rgba(137,135,129,.14)'
        : 'rgba(12,107,44,' + (0.18 + n * 0.48).toFixed(2) + ')';
      var border = top == null ? '1px dashed rgba(82,81,78,.55)' : '1px solid rgba(10,92,38,.62)';

      var icon = L.divIcon({
        className: 'pin', iconSize: [d, d], iconAnchor: [d / 2, d / 2],
        html: '<i style="background:' + fill + ';border:' + border + '">' +
          (list.length > 1 ? list.length : '') + '</i>'
      });

      // Three is the most a bubble can hold before it covers the block it is
      // pinned to; the venue page has the rest.
      var rows = list.slice(0, 3).map(function (e) {
        return '<span class="pop-ev" role="button" tabindex="0" data-ev="' + e.id + '">' +
          esc(e.name) + (isJoinable(e) ? ' <span class="gl-series">↻</span>' : '') +
          '<br><span class="w">' + esc(fmtShort(e.date)) +
          (fmtTime(e) ? ' · ' + esc(fmtTime(e)) : '') +
          (typeof e.quality === 'number' ? ' · ' + e.quality.toFixed(1) : '') + '</span></span>';
      }).join('');
      var more = list.length > 3
        ? '<div class="ml pop-more">+' + (list.length - 3) + ' more</div>' : '';

      L.marker([v.lat, v.lng], { icon: icon, title: v.name })
        .bindPopup('<div class="pop-t"><a href="venue.html?id=' + encodeURIComponent(vid) + '">' +
          esc(v.name) + '</a></div>' +
          '<div class="ml">' + list.length + (list.length === 1 ? ' listing' : ' listings') +
          (top != null ? ' · top ' + top.toFixed(1) : '') + '</div>' + rows + more)
        .addTo(pinLayer);
    });
  }

  function renderPanel() {
    var el = $('panel');
    if (!el) return;
    if (zone === 'all') { el.classList.remove('show'); return; }
    var evs = filteredEvents().filter(function (e) { return hoodOf(e) === zone; }).sort(chrono);
    var rows = evs.length ? evs.map(function (e) {
      return '<div class="pev" role="button" tabindex="0" data-ev="' + e.id + '">' +
        '<span class="pev-name">' + esc(e.name) + '</span>' +
        (isJoinable(e) ? '<span class="gl-series">↻</span>' : '') +
        '<div class="pev-meta">' + esc(fmtDayHead(e.date)) +
        (fmtTime(e) ? ' · ' + esc(fmtTime(e)) : '') +
        (e.venue ? ' · ' + venueLink(e) : '') +
        (typeof e.quality === 'number' ? ' · <span class="q">' + e.quality.toFixed(1) + '</span>' : '') +
        '</div></div>';
    }).join('') : '<p class="panel-empty">Nothing here in these dates. Try fewer filters — ' +
      'or the Bulletin has not found this neighborhood\'s calendars yet.</p>';

    el.innerHTML = '<div class="panel-head"><h2>' + esc(zone) + '</h2>' +
      '<span class="ml">' + evs.length + (evs.length === 1 ? ' listing' : ' listings') +
      ' · ' + esc(F.day ? dayChoiceLabel(F.day) : WIN.label) + '</span>' +
      '<button class="panel-x" type="button" data-hood="all" title="Show all of ' + esc(CITY_LABEL) + '" aria-label="Show all of ' + esc(CITY_LABEL) + '">✕</button>' +
      '</div><div class="panel-list">' + rows + '</div>';
    el.classList.add('show');
  }

  var refreshTimer = null;
  function mapRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(function () { buildPins(); renderPanel(); }, 90);
  }

  /* ── the list/map switch ───────────────────────────────────────────── */

  function refreshView() {
    if (curView() === 'map') mapRefresh(); else renderBulletin();
  }

  // Leaflet only rides along once someone actually opens the map — list-only
  // readers never fetch it.
  var leafletReq = null;
  function ensureLeaflet(cb) {
    if (window.L) { cb(); return; }
    if (leafletReq) { leafletReq.push(cb); return; }
    leafletReq = [cb];
    var pending = 2;
    function done() {
      if (--pending) return;
      var q = leafletReq;
      leafletReq = null;
      if (window.L) { q.forEach(function (f) { f(); }); return; }
      var m = $('map');
      if (m) m.innerHTML = '<p class="note" style="margin:20px">The map failed to load. ' +
        'Refresh the page and try again.</p>';
    }
    var link = document.createElement('link');
    link.rel = 'stylesheet'; link.href = (CITY.assetBase || '') + 'vendor/leaflet/leaflet.css';
    link.onload = link.onerror = done;
    document.head.appendChild(link);
    var s = document.createElement('script');
    s.src = (CITY.assetBase || '') + 'vendor/leaflet/leaflet.js';
    s.onload = s.onerror = done;
    document.head.appendChild(s);
  }

  function setView(v) {
    VIEW = v === 'map' ? 'map' : 'list';
    // the map's fullscreen layout is the mappage class; toggling it is the
    // whole switch as far as CSS is concerned
    document.body.classList.toggle('mappage', VIEW === 'map');
    document.querySelectorAll('.viewswitch [data-view]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.dataset.view === VIEW ? 'true' : 'false');
    });
    try { sessionStorage.setItem((CITY.storeNs || 'pb') + '.view', VIEW); } catch (err) { /* private mode */ }
    // ?view=map keeps the map shareable now that map.html is gone
    if ($('map') && $('bulletin') && history.replaceState) {
      history.replaceState(null, '', VIEW === 'map' ? '?view=map' : location.pathname);
    }
    renderFilters();
    if (VIEW === 'map') {
      ensureLeaflet(function () {
        if (!map) { initMap(); return; }
        map.invalidateSize();
        buildPins();
        applyZone(false);
      });
    } else {
      renderBulletin();
    }
  }

  /* ── wiring ────────────────────────────────────────────────────────── */

  // the clicks the shared core doesn't own: the view switch, the map's hood
  // buttons, "clear the filters", and the advanced controls in the board head
  function wireClicks() {
    document.addEventListener('click', function (ev) {
      // links nested inside clickable rows navigate; the row action must not
      // swallow them
      if (ev.target.closest('a')) return;
      var vw = ev.target.closest('[data-view]');
      if (vw) { setView(vw.dataset.view); return; }
      var cat = ev.target.closest('.catchips [data-cat]');
      if (cat) {
        F.cat = F.cat === cat.dataset.cat ? '' : cat.dataset.cat;
        saveFilters(); syncCatChips(); refreshView(); return;
      }
      var hb = ev.target.closest('[data-hood]');
      if (hb) { if (map) { map.closePopup(); } setZone(hb.dataset.hood); return; }
      var clear = ev.target.closest('[data-clear]');
      if (clear) {
        F.free = F.solo = F.join = false; F.minq = 0; F.query = ''; F.hood = 'all'; F.hoodChosen = true; F.cat = '';
        saveFilters(); renderFilters(); syncCatChips(); renderBulletin(); return;
      }
      // the list's advanced settings live in the board's day header, so their
      // clicks land here rather than on a filter-bar host
      if (ev.target.closest('#bulletin')) {
        var ab = ev.target.closest('button');
        if (ab) {
          if (ab.dataset.adv) F.advOpen = !F.advOpen;
          else if (ab.dataset.flag) F[ab.dataset.flag] = !F[ab.dataset.flag];
          else if (ab.dataset.minq != null) F.minq = +ab.dataset.minq;
          else if (ab.dataset.resetAdvanced) { F.free = F.solo = F.join = false; F.minq = 0; }
          else ab = null;
          if (ab) {
            var wasAdv = !!ab.dataset.adv;
            saveFilters(); renderBulletin();
            // the re-render replaces the button that was just clicked
            if (wasAdv) { var again = document.querySelector('#bulletin .advtoggle'); if (again) again.focus(); }
          }
        }
      }
    });
  }

  PB.init = function () {
    DB = PB.DB; HOODS = PB.HOODS; F = PB.F; WIN = PB.WIN; venueById = PB.venueById;
    zone = F.hood;
    renderFilters();
    syncCatChips();
    wireFilters(refreshView);
    wireClicks();
    // the map is a view of the bulletin: ?view=map deep-links it, and the
    // session remembers which side of the switch you were on
    var wantMap = new URLSearchParams(location.search).get('view') === 'map';
    if (!wantMap && $('map') && $('bulletin')) {
      var savedView = null;
      try { savedView = sessionStorage.getItem((CITY.storeNs || 'pb') + '.view'); } catch (err) { /* ignore */ }
      wantMap = savedView === 'map';
    }
    setView($('map') && (wantMap || !$('bulletin')) ? 'map' : 'list');
  };
})();
