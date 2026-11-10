// Service worker: makes the installed app work offline.
//  * builds (v/...), libraries (lib/...) and models (models/...) never change at a given path -> cache first
//  * launcher, releases.json, install page -> network first (so testers see new builds), cache when offline
const APP = "lt-app";
const MODELS = "lt-models";
const SHELL = ["./", "index.html", "install.html", "manifest.webmanifest", "releases.json",
               "icons/icon-180.png", "icons/icon-192.png", "icons/icon-512.png"];

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(APP).then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => {})))));
});

self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

// a build sends its file list so the whole build is available offline
self.addEventListener("message", (e) => {
  if (e.data?.type !== "precache") return;
  e.waitUntil(caches.open(APP).then(async (c) => {
    for (const u of e.data.urls || []) {
      if (!(await caches.match(u))) await c.add(u).catch(() => {});
    }
  }));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || req.headers.has("range")) return;
  const rel = req.url.slice(self.registration.scope.length);
  if (/^(v|lib|models)\//.test(rel)) {
    e.respondWith((async () => {
      const hit = await caches.match(req, { ignoreSearch: true });
      if (hit) return hit;
      const res = await fetch(req);
      if (res.ok && res.type === "basic") {
        const copy = res.clone();
        caches.open(rel.startsWith("models/") ? MODELS : APP).then((c) => c.put(req, copy)).catch(() => {});
      }
      return res;
    })());
    return;
  }
  e.respondWith((async () => {
    try {
      const res = await fetch(req, { cache: "no-store" });
      if (res.ok) {
        const copy = res.clone();
        caches.open(APP).then((c) => c.put(req, copy)).catch(() => {});
      }
      return res;
    } catch (err) {
      const hit = await caches.match(req, { ignoreSearch: true });
      if (hit) return hit;
      throw err;
    }
  })());
});
