/* GEO RADIO - offline helper
 * Keeps the page + pictures on your computer so the site loads even on a bad modem day.
 * Radio streams are NEVER touched here - they always come straight from the station. */
var CACHE_NAME = "geo-radio-v3";
var ASSETS = [
  "./",
  "./index.html",
  "./manifest.json",
  "./favicon.ico",
  "./picmix.com_68043.gif",
  "./images/stars.jpg",
  "./images/title.gif",
  "./images/flame.gif",
  "./images/bullet.gif",
  "./images/new.gif",
  "./images/rainbow.gif",
  "./images/construction.gif",
  "./images/email.gif",
  "./images/dino.gif",
  "./images/btn_800.gif",
  "./images/btn_notepad.gif",
  "./images/btn_llama.gif",
  "./images/btn_56k.gif",
  "./images/btn_y2k.gif",
  "./images/btn_linkme.gif",
  "./images/icon-192.png",
  "./images/icon-512.png",
  "./images/counter/0.gif", "./images/counter/1.gif", "./images/counter/2.gif", "./images/counter/3.gif",
  "./images/counter/4.gif", "./images/counter/5.gif", "./images/counter/6.gif", "./images/counter/7.gif",
  "./images/counter/8.gif", "./images/counter/9.gif"
];

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) { return cache.addAll(ASSETS); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) { if (k !== CACHE_NAME) return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  var url = new URL(req.url);

  // only our own files; leave radio streams, other sites and range requests alone
  if (req.method !== "GET" || url.origin !== self.location.origin || req.headers.has("range")) return;

  // the page itself: try the network first so updates show up, fall back to the cached copy
  if (req.mode === "navigate" || url.pathname.slice(-5) === ".html" || url.pathname.slice(-1) === "/") {
    e.respondWith(
      fetch(req).then(function (res) {
        var copy = res.clone();
        caches.open(CACHE_NAME).then(function (c) { c.put(req, copy); });
        return res;
      }).catch(function () {
        return caches.match(req).then(function (hit) { return hit || caches.match("./index.html"); });
      })
    );
    return;
  }

  // pictures etc: cache first
  e.respondWith(
    caches.match(req).then(function (hit) { return hit || fetch(req); })
  );
});
