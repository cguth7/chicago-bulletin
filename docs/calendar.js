/* The Bulletin — the calendar (calendar.html): the week as one
   printable page. Loaded after app.js, the shared core; everything here
   comes off window.PB. */
(function () {
  'use strict';

  var PB = window.PB;
  var $ = PB.$, esc = PB.esc, MON = PB.MON, DOW = PB.DOW;
  var todayISO = PB.todayISO, dateObj = PB.dateObj, shiftISO = PB.shiftISO,
    fmtDayHead = PB.fmtDayHead, fmtTime = PB.fmtTime;
  var isFree = PB.isFree, isJoinable = PB.isJoinable, rank = PB.rank, hoodOf = PB.hoodOf;

  // loaded state, attached to PB by the core just before init runs
  var DB, HOODS, WIN;

  var CITY = window.CITY_CONFIG || {};   // philly defaults when absent (see app.js)
  var ELSEWHERE = CITY.elsewhere || 'Elsewhere in Philadelphia';

  // A cell shows at most this many listings — the best of the day, by
  // quality — so a busy hood can't stretch its whole row (or a printed
  // page) without limit. The rest collapse to a "+ n more" line. A single
  // neighborhood has a whole page to itself, so its cap is roomier.
  var CAL_MAX = 5, CAL_MAX_ONE = 9;

  // Which neighborhood the calendar (and its printout) shows; 'all' is the
  // full matrix. Deliberately not the shared F.hood — picking a hood to
  // print shouldn't re-aim the bulletin and the map.
  var calHood = 'all';

  // Row order follows the geojson tour order (West Philly → South Philly →
  // Fishtown); hoods without geometry sort after, Elsewhere always last.
  function hoodOrders() {
    var ord = {};
    if (HOODS) HOODS.features.forEach(function (f) {
      var o = f.properties && f.properties.order;
      if (typeof o !== 'number') o = 900;
      // a region group spans several features; the group sorts at its
      // earliest member so it lands where the tour first reaches it
      var name = f.properties.group || f.properties.name;
      if (ord[name] == null || o < ord[name]) ord[name] = o;
    });
    return ord;
  }

  // Within a calendar cell: all-day first, then by clock, unknown time last.
  function calSort(a, b) {
    var ta = a.all_day ? '00:00' : (a.time || '99:99');
    var tb = b.all_day ? '00:00' : (b.time || '99:99');
    if (ta !== tb) return ta < tb ? -1 : 1;
    return String(a.name).localeCompare(String(b.name));
  }

  function calEvHTML(e) {
    var t = fmtTime(e);
    var q = typeof e.quality === 'number' ? e.quality.toFixed(1) : null;
    var top = '';
    if (t || q || isFree(e)) {
      top = '<span class="cal-t">' + esc(t) +
        (isFree(e) ? '<b class="cal-free">free</b>' : '') +
        (q ? '<span class="cal-q">' + q + '</span>' : '') + '</span>';
    }
    return '<div class="cal-ev" role="button" tabindex="0" data-ev="' + e.id + '">' + top +
      '<span class="cal-n">' + esc(e.name) +
      (isJoinable(e) ? ' <span class="gl-series" title="Happens regularly — you can join it">↻</span>' : '') +
      '</span>' +
      (e.venue ? '<span class="cal-v">' + esc(e.venue) + '</span>' : '') +
      '</div>';
  }

  function calRangeLabel(days) {
    if (!days.length) return '';
    var a = dateObj(days[0]), b = dateObj(days[days.length - 1]);
    return DOW[a.getDay()] + ' ' + MON[a.getMonth()] + ' ' + a.getDate() +
      ' – ' + DOW[b.getDay()] + ' ' + MON[b.getMonth()] + ' ' + b.getDate();
  }

  function renderCalendar() {
    var host = $('calendar');
    if (!host) return;

    // one column per day of the window, empty days included — a calendar
    // holds its shape even when a day is quiet
    var days = [], d = WIN.start;
    while (d <= WIN.end && days.length < 14) { days.push(d); d = shiftISO(d, 1); }

    var evs = DB.events.filter(function (e) {
      return e.date && e.date >= WIN.start && e.date <= WIN.end;
    });

    var range = calRangeLabel(days);
    var cr = $('cal-range'); if (cr) cr.textContent = range;
    var pr = $('print-range');
    if (pr) pr.textContent = (calHood === 'all' ? '' : calHood + ' · ') + range;
    var pb = $('print-btn');
    if (pb && !pb.dataset.wired) {
      pb.dataset.wired = '1';
      pb.addEventListener('click', function () { window.print(); });
    }

    var h = '';
    if (WIN.note) h += '<p class="note">' + esc(WIN.note) + '</p>';
    if (!evs.length) {
      host.innerHTML = h + '<p class="note empty">Nothing on the calendar right now. ' +
        'Check back after the next export.</p>';
      return;
    }

    var byHood = {};
    evs.forEach(function (e) {
      var hood = hoodOf(e) || ELSEWHERE;
      var slot = byHood[hood] || (byHood[hood] = { n: 0, days: {} });
      (slot.days[e.date] || (slot.days[e.date] = [])).push(e);
      slot.n++;
    });
    var ord = hoodOrders();
    var hoods = Object.keys(byHood).sort(function (a, b) {
      var oa = a === ELSEWHERE ? 9999 : (ord[a] != null ? ord[a] : 900);
      var ob = b === ELSEWHERE ? 9999 : (ord[b] != null ? ord[b] : 900);
      return oa - ob || String(a).localeCompare(String(b));
    });

    var pick = $('cal-hood');
    if (pick) {
      if (calHood !== 'all' && !byHood[calHood]) calHood = 'all';
      // The picker lists busiest hoods first (rows below keep the tour order,
      // which is what makes the printed matrix walkable). Elsewhere stays last.
      var pickHoods = hoods.slice().sort(function (a, b) {
        if (a === ELSEWHERE) return 1;
        if (b === ELSEWHERE) return -1;
        return byHood[b].n - byHood[a].n || String(a).localeCompare(String(b));
      });
      pick.innerHTML = '<option value="all">All of ' + esc(CITY.cityLabel || 'Philly') + '</option>' +
        pickHoods.map(function (hood) {
          return '<option value="' + esc(hood) + '"' +
            (calHood === hood ? ' selected' : '') + '>' + esc(hood) + '</option>';
        }).join('');
      if (!pick.dataset.wired) {
        pick.dataset.wired = '1';
        pick.addEventListener('change', function () {
          calHood = pick.value;
          renderCalendar();
        });
      }
    }
    var shownHoods = calHood === 'all' ? hoods : [calHood];
    var cap = shownHoods.length === 1 ? CAL_MAX_ONE : CAL_MAX;
    // one row prints bigger than the matrix; the stylesheet keys off this
    document.body.classList.toggle('cal-one', shownHoods.length === 1);

    var today = todayISO();
    h += '<div class="cal"><div class="cal-head"><div>Neighborhood</div>';
    days.forEach(function (day) {
      h += '<div' + (day === today ? ' class="cal-today-h"' : '') + '>' +
        esc(fmtDayHead(day)) + '</div>';
    });
    h += '</div>';

    // On the one-hood printout the first column has a page of blank space
    // under the hood name — use it to say where the calendar came from.
    var qrBox = shownHoods.length !== 1 ? '' :
      '<div class="cal-qr" aria-hidden="true">' + (CITY.calFooterHtml ||
        '<span class="cal-qr-brand">' + esc(CITY.siteName || 'The Bulletin') + '</span>') +
      '</div>';

    var shown = 0, capped = false;
    shownHoods.forEach(function (hood) {
      var slot = byHood[hood];
      h += '<div class="cal-row"><div class="cal-hoodcell">' +
        '<span class="cal-hood-name">' + esc(hood) + '</span>' +
        '<span class="cal-hood-n">' + slot.n + (slot.n === 1 ? ' listing' : ' listings') + '</span>' +
        qrBox + '</div>';
      days.forEach(function (day) {
        var all = slot.days[day] || [];
        // keep the day's best `cap` by quality, then lay them out by time
        var list = all.slice().sort(rank).slice(0, cap).sort(calSort);
        shown += list.length;
        var more = all.length - list.length;
        if (more > 0) capped = true;
        h += '<div class="cal-cell' + (day === today ? ' cal-today' : '') +
          (list.length ? '' : ' is-empty') + '">' +
          '<div class="cal-dlab">' + esc(fmtDayHead(day)) + '</div>' +
          list.map(calEvHTML).join('') +
          (more > 0 ? '<div class="cal-more">+ ' + more + ' more</div>' : '') +
          '</div>';
      });
      h += '</div>';
    });

    h += '<div class="board-foot cal-foot">' + shown +
      (shown === 1 ? ' listing' : ' listings') +
      (calHood === 'all'
        ? ' · ' + shownHoods.length + ' neighborhoods · rows run west to east across the city'
        : ' · ' + esc(calHood)) +
      (capped ? ' · the best ' + cap + ' a day, full list on the bulletin' : '') +
      ' · open a listing for the reasons</div></div>';
    host.innerHTML = h;
  }

  PB.init = function () {
    DB = PB.DB; HOODS = PB.HOODS; WIN = PB.WIN;
    // ?hood= makes a neighborhood's printable week a shareable link;
    // renderCalendar falls back to 'all' if the name doesn't resolve
    var qh = new URLSearchParams(location.search).get('hood');
    if (qh) calHood = qh;
    renderCalendar();
  };
})();
