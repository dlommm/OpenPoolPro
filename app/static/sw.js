const CACHE = "cpp-static-v2";
const ASSETS = [
  "/static/htmx.min.js",
  "/static/icons/icon-192.png",
  "/static/fonts/barlow-condensed-600.woff2",
  "/static/fonts/barlow-condensed-700.woff2",
  "/static/fonts/plex-sans-var.woff2",
  "/static/fonts/plex-mono-500.woff2",
  "/static/fonts/plex-mono-600.woff2",
];
// Assets that change between deploys use network-first so a stale cache
// can never style a newer page; the cache is only an offline fallback.
const NETWORK_FIRST = ["/static/app.css"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS.concat(NETWORK_FIRST))));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (!url.pathname.startsWith("/static/")) return;
  if (NETWORK_FIRST.includes(url.pathname)) {
    e.respondWith(
      fetch(e.request)
        .then(resp => {
          const copy = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
          return resp;
        })
        .catch(() => caches.match(e.request)));
  } else {
    e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
  }
});
