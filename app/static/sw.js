// VERSION is substituted with config.APP_VERSION when /sw.js is served, so the
// worker's bytes change every release -> the browser detects a new worker and
// activate() purges the previous version's caches (both derive from VERSION).
const VERSION = "__APP_VERSION__";
const CACHE = "cpp-static-" + VERSION;
const PAGES_CACHE = "cpp-pages-" + VERSION;
const QUEUE_DB = "cpp-queue";
const QUEUE_STORE = "posts";

const ASSETS = [
  "/static/htmx.min.js",
  "/static/icons/icon-192.png",
  "/static/fonts/barlow-condensed-600.woff2",
  "/static/fonts/barlow-condensed-700.woff2",
  "/static/fonts/plex-sans-var.woff2",
  "/static/fonts/plex-mono-500.woff2",
  "/static/fonts/plex-mono-600.woff2",
  "/static/offline.html",
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
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE && k !== PAGES_CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
      // iOS Safari has no Background Sync API. `activate` fires on every
      // worker install (i.e. effectively every app open after an update,
      // and once per session otherwise) so it doubles as a replay trigger
      // alongside the `message` listener below -- the `sync` listener at
      // the bottom is a bonus for browsers that do support Background Sync.
      .then(() => replay()));
});

self.addEventListener("fetch", e => {
  const req = e.request;
  const url = new URL(req.url);

  if (url.pathname.startsWith("/static/")) {
    if (NETWORK_FIRST.includes(url.pathname)) {
      e.respondWith(
        fetch(req)
          .then(resp => {
            const copy = resp.clone();
            caches.open(CACHE).then(c => c.put(req, copy));
            return resp;
          })
          .catch(() => caches.match(req)));
    } else {
      e.respondWith(caches.match(req).then(hit => hit || fetch(req)));
    }
    return;
  }

  if (req.method === "POST" && isQueueableVisitPost(req, url)) {
    e.respondWith(handleQueueablePost(req, url));
    return;
  }

  if (req.method === "GET" && req.mode === "navigate") {
    e.respondWith(handleNavigate(req));
    return;
  }
});

function isQueueableVisitPost(req, url) {
  if (!url.pathname.startsWith("/visit/")) return false;
  if (!(url.pathname.endsWith("/complete") || url.pathname.endsWith("/skip"))) return false;
  const contentType = req.headers.get("content-type") || "";
  return contentType.includes("application/x-www-form-urlencoded");
}

async function handleQueueablePost(req, url) {
  // Clone BEFORE attempting the network: a Request/Response body stream
  // can only be read once, and we need the raw urlencoded text if fetch
  // throws (offline/network error). fetch() rejecting is a network error;
  // an HTTP 4xx/5xx response is NOT an error here and is passed through
  // as-is (the server was reachable -- nothing to queue).
  const bodyClone = req.clone();
  try {
    return await fetch(req);
  } catch (err) {
    const body = await bodyClone.text();
    await queueAdd(url.pathname + url.search, body);
    return Response.redirect("/?queued=1", 303);
  }
}

async function handleNavigate(req) {
  try {
    const resp = await fetch(req);
    if (resp && resp.ok) {
      const copy = resp.clone();
      caches.open(PAGES_CACHE).then(c => c.put(req, copy));
    }
    return resp;
  } catch (err) {
    // Exact match first (the common case: reopening a page whose URL,
    // including query string, was cached verbatim). Fall back to an
    // ignoreSearch match so a navigation whose query string was never
    // cached under that exact key -- e.g. the `?queued=1` redirect target
    // above -- still hits the cached page instead of falling through to
    // the generic offline.html.
    const hit = await caches.match(req) || await caches.match(req, { ignoreSearch: true });
    return hit || caches.match("/static/offline.html");
  }
}

// ---------- IndexedDB queue (cpp-queue / posts) ----------

function openQueueDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(QUEUE_DB, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(QUEUE_STORE)) {
        db.createObjectStore(QUEUE_STORE, { keyPath: "id", autoIncrement: true });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function queueAdd(url, body) {
  return openQueueDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, "readwrite");
    tx.objectStore(QUEUE_STORE).add({ url, body, ts: Date.now() });
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  }));
}

function queueAll() {
  return openQueueDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, "readonly");
    const req = tx.objectStore(QUEUE_STORE).getAll();
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  }));
}

function queueDelete(id) {
  return openQueueDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, "readwrite");
    tx.objectStore(QUEUE_STORE).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  }));
}

function queueCount() {
  return openQueueDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, "readonly");
    const req = tx.objectStore(QUEUE_STORE).count();
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  }));
}

// FIFO replay: posts each queued entry in enqueue order. Deletes the entry
// on ANY HTTP response (2xx-5xx alike -- the server was reachable, so it's
// no longer this queue's problem). Stops at the first network error (we
// went offline again mid-flush) so it and every entry after it stay queued
// in order for the next trigger. Always ends by telling every open client
// the current count, whether or not anything actually replayed.
async function replay() {
  let entries = [];
  try {
    entries = await queueAll();
  } catch (err) {
    entries = [];
  }
  entries.sort((a, b) => a.id - b.id);
  for (const entry of entries) {
    let resp;
    try {
      resp = await fetch(entry.url, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: entry.body,
        credentials: "include",
      });
    } catch (err) {
      break;  // genuine network error — went offline again; keep this + the rest queued
    }
    // Only drop the entry on a real success. An expired/invalidated session
    // (e.g. the owner ended sessions while a tech was offline) makes the auth
    // middleware answer this POST with a 303 -> /login, which fetch follows to
    // a 200 login page. Deleting on that would silently destroy the tech's
    // completed-visit readings. Keep it queued (and stop) so it flushes once
    // the tech logs back in; never delete on a redirect-to-login or non-2xx.
    const finalPath = (() => { try { return new URL(resp.url).pathname; } catch { return ""; } })();
    if (resp.ok && finalPath !== "/login") {
      await queueDelete(entry.id);
    } else {
      break;
    }
  }
  const count = await queueCount().catch(() => 0);
  const clients = await self.clients.matchAll();
  clients.forEach(client => client.postMessage({ type: "queue-count", count }));
}

// Background Sync -- supported on Chrome/Android, NOT on iOS Safari. The
// `activate` handler above and the `message` listener below are the real
// replay triggers; this is a bonus for browsers that do fire it.
self.addEventListener("sync", e => {
  if (e.tag === "cpp-flush") {
    e.waitUntil(replay());
  }
});

self.addEventListener("message", e => {
  if (e.data && e.data.type === "flush-queue") {
    e.waitUntil(replay());
  }
});
