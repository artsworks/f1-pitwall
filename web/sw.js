const CACHE = "pitwall-static-v2";
const ASSETS = ["/static/style.css", "/static/app.js", "/static/manifest.webmanifest"];
self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)));
  self.skipWaiting();
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key !== CACHE).map(key => caches.delete(key))
  )));
  self.clients.claim();
});
self.addEventListener("fetch", event => {
  if (!ASSETS.includes(new URL(event.request.url).pathname)) return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
