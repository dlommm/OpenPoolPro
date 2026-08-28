// VERSION is substituted with config.APP_VERSION when /sw.js is served, so the
// worker's bytes change every release -> the browser detects a new worker and
// activate() purges the previous version's caches (both derive from VERSION).
const VERSION = "__APP_VERSION__";
const CACHE = "cpp-static-" + VERSION;
const PAGES_CACHE = "cpp-pages-" + VERSION;
const QUEUE_DB = "cpp-queue";
const QUEUE_STORE = "posts";
// Entries the server has DEFINITIVELY rejected park here instead of staying at
// the head of the FIFO. A 404/400/409/422 can never succeed on a retry, so
// leaving it queued buried every completion behind it forever behind a silent
// "3 waiting" badge. The full payload is kept -- a tech's readings are only
// destroyed by an explicit, confirmed Discard from the queue screen.
const FAILED_STORE = "failed";
// v1 shipped with only QUEUE_STORE; the bump creates FAILED_STORE on upgrade.
const QUEUE_DB_VERSION = 2;

// Queued URL -> {label, page} for the queue screen. Written synchronously by
// the `queue-hint` message the page sends immediately BEFORE submitting a
// completion form, and read by the fetch handler in the same worker turn -- an
// async IndexedDB write would lose that race. Losing a hint (worker restart) is
// harmless: the queue screen falls back to the URL.
const QUEUE_HINTS = {};

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
    const hint = QUEUE_HINTS[url.pathname] || {};
    delete QUEUE_HINTS[url.pathname];
    await queueAdd(url.pathname + url.search, body, hint.page || refPath(req.referrer), hint.label || "");
    return Response.redirect("/?queued=1", 303);
  }
}

// The queued URL only carries the VISIT id, but the page that submitted it is
// /visit/<property>/<date> -- which is character-for-character the href the
// Today page renders for that stop. Recording it is what lets the page overlay
// a "pending sync" state on the right card without a server round-trip.
function refPath(referrer) {
  try {
    return new URL(referrer).pathname;
  } catch (err) {
    return "";
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

// ---------- IndexedDB queue (cpp-queue / posts + failed) ----------

function openQueueDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(QUEUE_DB, QUEUE_DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      // Both guarded by `contains` so the v1 -> v2 upgrade of an installed app
      // adds only the missing store and never touches queued work in `posts`.
      if (!db.objectStoreNames.contains(QUEUE_STORE)) {
        db.createObjectStore(QUEUE_STORE, { keyPath: "id", autoIncrement: true });
      }
      if (!db.objectStoreNames.contains(FAILED_STORE)) {
        db.createObjectStore(FAILED_STORE, { keyPath: "id", autoIncrement: true });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function storeAdd(store, record) {
  return openQueueDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(store, "readwrite");
    tx.objectStore(store).add(record);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  }));
}

function storeAll(store) {
  return openQueueDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(store, "readonly");
    const req = tx.objectStore(store).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  }));
}

function storeGet(store, id) {
  return openQueueDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(store, "readonly");
    const req = tx.objectStore(store).get(id);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => reject(req.error);
  }));
}

function storeDelete(store, id) {
  return openQueueDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(store, "readwrite");
    tx.objectStore(store).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  }));
}

// `page` and `label` are queue-screen metadata only -- the replayed POST still
// sends exactly the urlencoded body the form produced, unchanged.
function queueAdd(url, body, page, label) {
  return storeAdd(QUEUE_STORE, { url, body, page: page || "", label: label || "", ts: Date.now() });
}

function queueAll() {
  return storeAll(QUEUE_STORE);
}

function queueDelete(id) {
  return storeDelete(QUEUE_STORE, id);
}

function failedAll() {
  return storeAll(FAILED_STORE);
}

// Park a definitively-rejected entry: payload intact, plus the status and the
// moment it died so the queue screen can explain WHY it is not syncing.
async function failEntry(entry, status) {
  try {
    await storeAdd(FAILED_STORE, {
      url: entry.url,
      body: entry.body,
      page: entry.page || "",
      label: entry.label || "",
      ts: entry.ts,
      status: status,
      failed_at: Date.now(),
    });
  } catch (err) {
    return;  // couldn't park it -- leave it queued rather than lose the readings
  }
  await queueDelete(entry.id);
}

// Retry puts the entry back at the TAIL, not the head: it already lost its place
// in line, and everything that was stuck behind it has long since gone through.
async function retryFailed(id) {
  const entry = await storeGet(FAILED_STORE, id).catch(() => null);
  if (entry) {
    await queueAdd(entry.url, entry.body, entry.page, entry.label);
    await storeDelete(FAILED_STORE, id).catch(() => {});
  }
  await replay();
}

async function discardFailed(id) {
  await storeDelete(FAILED_STORE, id).catch(() => {});
  await postQueueState();
}

// One message carries both the counts (badge) and the rows (queue screen), so
// any client repaint is a single round trip. `count` keeps its original meaning
// -- pending entries -- so an older page shell still shows a correct badge.
// Bodies are deliberately NOT sent: the page has no use for them and they can
// hold a customer's readings.
async function postQueueState() {
  const pending = await queueAll().catch(() => []);
  const failed = await failedAll().catch(() => []);
  pending.sort((a, b) => a.id - b.id);
  failed.sort((a, b) => a.id - b.id);
  const msg = {
    type: "queue-count",
    count: pending.length,
    failed: failed.length,
    pending: pending.map(describe),
    failedEntries: failed.map(describe),
    // "auth" when the last replay stopped on a redirect to /login — the page
    // renders a "log back in" notice instead of a bare "waiting to send".
    blocked: BLOCKED_AUTH ? "auth" : "",
  };
  const clients = await self.clients.matchAll();
  clients.forEach(client => client.postMessage(msg));
}

function describe(entry) {
  return {
    id: entry.id,
    url: entry.url,
    page: entry.page || "",
    label: entry.label || "",
    ts: entry.ts || 0,
    status: entry.status || 0,
    failed_at: entry.failed_at || 0,
  };
}

// FIFO replay: posts each queued entry in enqueue order. Every response falls
// into exactly one of three buckets, and the distinction matters because the
// head of a FIFO gates everything behind it:
//
//   success    -> delete the entry and keep draining.
//   retryable  -> STOP. A network error means we went offline again mid-flush;
//                 a 5xx means the server is having a bad minute; a redirect to
//                 /login means the session expired. All three can succeed on a
//                 later trigger, and stopping preserves enqueue order.
//   dead       -> a 4xx that is NOT the login redirect. 404 = the visit was
//                 deleted while the tech was offline; 400/409/422 = the server
//                 will reject this exact body every single time. Retrying it
//                 forever cannot help and buries every completion behind it, so
//                 it moves to the failed store (payload + status kept, visible
//                 and retryable/discardable from the queue screen) and the
//                 replay CONTINUES.
//
// Always ends by telling every open client the current queue state, whether or
// not anything actually replayed.
//
// Single-flight latch: replay is triggered from four places (activate, the
// flush-queue message, Background Sync, retryFailed) and two overlapping runs
// could each POST the head entry before either deletes it — the server would
// receive the same completion twice. Overlapping callers share the run already
// in flight instead.
let REPLAY_INFLIGHT = null;
// Sticky "the session died mid-replay" marker, reported to clients via
// postQueueState as blocked:"auth" so the queue screen can say "log in" instead
// of an eternal unexplained "waiting to send". Recomputed on every run: any
// replay that gets past the login redirect clears it.
let BLOCKED_AUTH = false;

function replay() {
  if (!REPLAY_INFLIGHT) {
    REPLAY_INFLIGHT = replayOnce().finally(() => { REPLAY_INFLIGHT = null; });
  }
  return REPLAY_INFLIGHT;
}

async function replayOnce() {
  BLOCKED_AUTH = false;
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
    // An expired/invalidated session (e.g. the owner ended sessions while a
    // tech was offline) makes the auth middleware answer this POST with a
    // 303 -> /login, which fetch follows to a 200 login page. Treating that as
    // success would silently destroy the tech's completed-visit readings, and
    // treating it as dead would do the same more slowly -- it is retryable, so
    // it stays queued until the tech logs back in.
    const finalPath = (() => { try { return new URL(resp.url).pathname; } catch { return ""; } })();
    const loginRedirect = finalPath === "/login";
    if (resp.ok && !loginRedirect) {
      await queueDelete(entry.id);
    } else if (loginRedirect || resp.status >= 500 || resp.status < 400) {
      if (loginRedirect) BLOCKED_AUTH = true;
      break;  // retryable — login redirect, server fault, or an odd non-2xx we won't gamble on
    } else {
      await failEntry(entry, resp.status);  // dead 4xx — park it, keep draining the rest
    }
  }
  await postQueueState();
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
  const data = e.data || {};
  if (data.type === "flush-queue") {
    e.waitUntil(replay());
  } else if (data.type === "queue-state") {
    // The queue screen asking what's in here. Answering costs no network, so it
    // works exactly the same offline -- which is the only time it matters.
    e.waitUntil(postQueueState());
  } else if (data.type === "queue-retry") {
    e.waitUntil(retryFailed(data.id));
  } else if (data.type === "queue-discard") {
    e.waitUntil(discardFailed(data.id));
  } else if (data.type === "queue-hint" && data.url) {
    // Deliberately synchronous (see QUEUE_HINTS): the POST this describes hits
    // the fetch handler in the very next turn.
    QUEUE_HINTS[data.url] = { label: data.label || "", page: data.page || "" };
  }
});
