const CACHE = "cpp-static-v1";
const ASSETS = ["/static/app.css", "/static/htmx.min.js", "/static/icons/icon-192.png"];
self.addEventListener("install", e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS))));
self.addEventListener("activate", e => e.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))));
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
  }
});
