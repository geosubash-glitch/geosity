// Reel Collage service worker.
// - Keeps the page, the vision library and its models on the phone after the
//   first visit, so later runs start fast and work offline.
// - Receives videos shared from the Android gallery (Web Share Target), one
//   or several at a time, and hands them to the page.
// Cache names all start with "reel-collage-" so this never touches the rest of
// the site's caches. Another worker on this site may clear ours; everything
// here re-downloads on demand, so that only costs a refetch.

const CACHE = 'reel-collage-v2';
const SHARE_CACHE = 'reel-collage-share';
const SHELL = ['./', './index.html', './app.js', './manifest.webmanifest', './icon-192.png', './apple-touch-icon.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('reel-collage-') && k !== CACHE && k !== SHARE_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function receiveShare(request) {
  const scope = self.registration.scope;
  try {
    const form = await request.formData();
    const files = form.getAll('video').filter((f) => f && typeof f !== 'string' && f.size > 0);
    if (files.length) {
      const cache = await caches.open(SHARE_CACHE);
      const batch = Date.now();
      for (let i = 0; i < files.length; i++) {
        await cache.put(`shared-video-${batch}-${i}`, new Response(files[i], {
          headers: { 'content-type': files[i].type || 'video/mp4', 'x-name': encodeURIComponent(files[i].name || 'video') },
        }));
      }
      return Response.redirect(new URL('./?shared=1', scope).href, 303);
    }
  } catch (err) {
    // fall through to the link message
  }
  return Response.redirect(new URL('./?link=1', scope).href, 303);
}

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;

  if (e.request.method === 'POST' && url.pathname.endsWith('/share')) {
    e.respondWith(receiveShare(e.request));
    return;
  }
  if (e.request.method !== 'GET' || e.request.headers.has('range')) return;

  if (url.pathname.includes('/vendor/') || /\.(png|bin)$/.test(url.pathname)) {
    // Big, versioned files: cache first.
    e.respondWith(
      caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
        return res;
      }))
    );
    return;
  }

  // The page and its script: network first so updates arrive, cache when offline.
  e.respondWith(
    fetch(e.request).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
      return res;
    }).catch(() => caches.match(e.request).then((hit) => hit || caches.match('./index.html')))
  );
});
