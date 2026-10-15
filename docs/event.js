/* The Bulletin — the event permalink page (event.html). Loaded after
   app.js, the shared core; everything here comes off window.PB. */
(function () {
  'use strict';

  var PB = window.PB;
  var $ = PB.$, esc = PB.esc;
  var todayISO = PB.todayISO, dateObj = PB.dateObj, fmtDayHead = PB.fmtDayHead,
    fmtShort = PB.fmtShort, longName = PB.longName, fmtTime = PB.fmtTime,
    dayChoiceLabel = PB.dayChoiceLabel;
  var JOINABLE = PB.JOINABLE, CADENCE = PB.CADENCE;
  var seriesOf = PB.seriesOf, isFree = PB.isFree, costLabel = PB.costLabel,
    hoodOf = PB.hoodOf, rank = PB.rank, hostOf = PB.hostOf,
    dimsHTML = PB.dimsHTML, evrowHTML = PB.evrowHTML;

  // loaded state, attached to PB by the core just before init runs
  var DB, venueById;

  // The bundle's datetimes are Philly-local with no zone; schema.org wants a
  // zoned startDate. Look up America/New_York's offset for that date so DST
  // resolves itself ("-04:00" / "-05:00").
  function etOffset(iso) {
    try {
      var parts = new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York', timeZoneName: 'longOffset'
      }).formatToParts(new Date(iso));
      for (var i = 0; i < parts.length; i++) {
        if (parts[i].type === 'timeZoneName') {
          var m = /GMT([+-]\d{2}:\d{2})/.exec(parts[i].value);
          if (m) return m[1];
        }
      }
    } catch (err) { }
    return '';
  }

  // schema.org/Event JSON-LD so crawlers and assistants can read a listing
  // without running this render. Ids last one export; so does the markup.
  function injectEventLD(e) {
    var v = e.venue_id ? venueById[e.venue_id] : null;
    var ld = {
      '@context': 'https://schema.org',
      '@type': 'Event',
      name: e.name,
      url: ((window.CITY_CONFIG || {}).siteOrigin || 'https://phillybulletin.com') +
        '/event.html?id=' + e.id
    };
    if (e.start) ld.startDate = e.start + etOffset(e.start);
    else if (e.date) ld.startDate = e.date;
    if (e.description) ld.description = e.description;
    if (v || e.venue) {
      var place = { '@type': 'Place', name: v ? v.name : e.venue };
      if (v && v.address) place.address = v.address;
      if (typeof e.lat === 'number' && typeof e.lng === 'number') {
        place.geo = { '@type': 'GeoCoordinates', latitude: e.lat, longitude: e.lng };
      }
      ld.location = place;
    }
    if (e.cost_min_cents != null || isFree(e)) {
      ld.offers = {
        '@type': 'Offer',
        price: e.cost_min_cents != null ? e.cost_min_cents / 100 : 0,
        priceCurrency: 'USD'
      };
      if (e.url) ld.offers.url = e.url;
    }
    var s = document.createElement('script');
    s.type = 'application/ld+json';
    s.textContent = JSON.stringify(ld);
    document.head.appendChild(s);
  }

  function renderEventDetail() {
    var host = $('event-detail');
    if (!host) return;
    var raw = new URLSearchParams(location.search).get('id');
    var e = null;
    if (raw != null && raw !== '') {
      for (var i = 0; i < DB.events.length; i++) {
        if (DB.events[i].id === +raw) { e = DB.events[i]; break; }
      }
    }
    if (!e) {
      host.innerHTML = '<p class="note">No listing with id <b>' + esc(raw || '(none)') +
        '</b> in this week’s bundle — permalinks only last as long as the export they came from. ' +
        '<a href="index.html">Back to the bulletin.</a></p>';
      return;
    }
    document.title = e.name + ' — ' + ((window.CITY_CONFIG || {}).siteName || 'The Philly Bulletin');
    var crumb = $('crumb-name'); if (crumb) crumb.textContent = e.name;
    injectEventLD(e);

    var sr = seriesOf(e);
    var hood = hoodOf(e);
    var t = fmtTime(e);
    var c = costLabel(e);
    var v = e.venue_id ? venueById[e.venue_id] : null;

    var meta = [];
    if (v) meta.push('<a href="venue.html?id=' + encodeURIComponent(v.id) + '">' + esc(v.name) + '</a>');
    else if (e.venue) meta.push(esc(e.venue));
    if (hood) meta.push(esc(hood));
    if (e.date) meta.push(esc(longName(dateObj(e.date))));
    if (t) meta.push(esc(t));
    if (isFree(e)) meta.push('<span class="free">Free</span>');
    else if (c) meta.push(esc(c));
    if (e.url) {
      meta.push('<a class="sheet-out" href="' + esc(e.url) +
        '" target="_blank" rel="noopener">See the listing ↗</a>');
    }

    var tagbits = '';
    if (isFree(e)) tagbits += '<span class="tag">free</span>';
    if (sr && JOINABLE[sr.cadence]) {
      tagbits += '<span class="tag plain">↻ ' + esc((CADENCE[sr.cadence] || sr.cadence).toLowerCase()) + '</span>';
    }
    if (e.all_day) tagbits += '<span class="tag plain">all day</span>';

    var h = '<header class="dhero"><div>' +
      (e.date ? '<span class="live' + (e.date < todayISO() ? ' plan' : '') + '">' +
        esc(dayChoiceLabel(e.date)) + '</span>' : '') +
      '<div class="titleline"><h1>' + esc(e.name) + '</h1></div>' +
      (meta.length ? '<div class="metaline">' + meta.join('<span class="dot">·</span>') + '</div>' : '') +
      (e.description ? '<p class="lede">' + esc(e.description) + '</p>' : '') +
      (tagbits ? '<div class="tags">' + tagbits + '</div>' : '') +
      '<div class="herostats">' +
      (e.date ? '<div><div class="l">When</div><div class="v">' + esc(fmtShort(e.date)) +
        (t ? ' · ' + esc(t) : '') + '</div></div>' : '') +
      '<div><div class="l">Cost</div><div class="v">' + (isFree(e) ? 'Free' : (c ? esc(c) : '—')) + '</div></div>' +
      '<div><div class="l">Quality</div><div class="v">' +
      (typeof e.quality === 'number' ? e.quality.toFixed(1) : '—') + '</div></div>' +
      '<div><div class="l">Series</div><div class="v">' +
      (sr ? esc(CADENCE[sr.cadence] || sr.cadence) : 'One-off') + '</div></div>' +
      '</div></div>' +
      '<aside class="factcard"><div class="hd"><span class="t">Listing facts</span>' +
      (typeof e.quality === 'number'
        ? '<span class="r"><b>' + e.quality.toFixed(1) + '</b> / 10</span>'
        : '<span class="r">not scored yet</span>') + '</div>' +
      (e.date ? '<div class="fact"><span>date</span><span class="v">' + esc(fmtDayHead(e.date)) + '</span></div>' : '') +
      (t ? '<div class="fact"><span>time</span><span class="v">' + esc(t) + '</span></div>' : '') +
      (v ? '<div class="fact"><span>venue</span><span class="v"><a href="venue.html?id=' +
        encodeURIComponent(v.id) + '">' + esc(v.name) + '</a></span></div>'
        : e.venue ? '<div class="fact"><span>venue</span><span class="v">' + esc(e.venue) + '</span></div>' : '') +
      // who runs a night is often the reason to go — link the group page when
      // the organizer resolved to a filed group, else just show the name
      (e.group_id ? '<div class="fact"><span>organizer</span><span class="v"><a href="group.html?id=' +
        encodeURIComponent(e.group_id) + '">' + esc(e.group || e.group_id) + '</a></span></div>'
        : e.group ? '<div class="fact"><span>organizer</span><span class="v">' + esc(e.group) + '</span></div>' : '') +
      (hood ? '<div class="fact"><span>neighborhood</span><span class="v">' + esc(hood) + '</span></div>' : '') +
      (isFree(e) ? '<div class="fact"><span>cost</span><span class="v">Free</span></div>'
        : c ? '<div class="fact"><span>cost</span><span class="v">' + esc(c) + '</span></div>' : '') +
      (sr ? '<div class="fact"><span>recurs</span><span class="v">' + esc(CADENCE[sr.cadence] || sr.cadence) + '</span></div>' : '') +
      '<div class="ft"><span>' + (e.url ? 'listing at <b>' + esc(hostOf(e.url)) + '</b>' : 'no listing link') +
      '</span><span class="sh">from this week’s export</span></div></aside></header>';

    h += '<div class="dmain one"><div class="dcontent"><section id="why">';
    if (e.scores) {
      h += '<div class="seclabel"><span>Quality — six dimensions, weighted</span>' +
        (typeof e.quality === 'number' ? '<span>' + e.quality.toFixed(1) + ' / 10 overall</span>' : '') +
        '</div><h2>Why it scores</h2>' + dimsHTML(e);
    } else {
      h += '<h2>Why it scores</h2>' +
        '<p class="unscored">Not scored yet. This one has not been read against the ' +
        'six questions — it still shows up, it just has no score.</p>';
    }
    h += '</section>';

    var others = DB.events.filter(function (o) { return o.id !== e.id; }).sort(rank).slice(0, 6);
    if (others.length) {
      h += '<section id="more"><div class="seclabel"><span>From the same week · best first</span>' +
        '<span><a href="index.html">the full bulletin →</a></span></div>' +
        '<h2>More this week</h2><div class="week">' + others.map(evrowHTML).join('') +
        '<div class="ft">ranked by community-weighted quality · tap a row for its six reasons</div></div></section>';
    }

    h += '</div></div>';
    host.innerHTML = h;
  }

  PB.init = function () {
    DB = PB.DB; venueById = PB.venueById;
    renderEventDetail();
  };
})();
