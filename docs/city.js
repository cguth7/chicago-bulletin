/* The Chicago Bulletin — the per-city config. Loaded before app.js on every
   page; the shared scripts read window.CITY_CONFIG. Change the city's look
   and map framing here (the engine's side of the same settings is the
   repo-root city.json). */
window.CITY_CONFIG = {
  siteName: 'The Chicago Bulletin',
  cityLabel: 'Chicago',
  // wherever the site is served from — localhost, GitHub Pages, a domain
  siteOrigin: window.location.origin + window.location.pathname.replace(/\/[^/]*$/, ''),
  storeNs: 'chi',                // session-storage namespace
  assetBase: '',                 // vendor/ sits beside the pages
  hoodsUrl: 'hoods.geojson',
  defaultHood: 'Loop',           // the map/list opens here (must be a hood name)
  allAliases: ['chicago', 'all chicago', 'chicago, il'],
  elsewhere: 'Elsewhere in Chicago',
  // the map's opening frame: downtown + the near neighborhoods
  mapCenter: [41.8925, -87.6345],
  coreBounds: [[41.857, -87.70], [41.95, -87.60]],
  maskOuter: [[41.2, -88.6], [41.2, -86.8], [42.6, -86.8], [42.6, -88.6]],
  // printable-calendar footer
  calFooterHtml: '<span class="cal-qr-brand">The Chicago Bulletin</span>'
};
