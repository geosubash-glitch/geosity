// Reel Collage service worker.
// - Keeps the page, the vision library and its models on the phone after the
//   first visit, so later runs start fast and work offline.
// - Receives videos shared from the Android gallery (Web Share Target) and
//   hands them to the page.
// Cache names all start with "reel-collage-" so this never touches the rest of
// the site's caches. Another worker on this site may clear ours; everything
// here re-downloads on demand, so that only costs a refetch.

const CACHE = 'reel-collage-v1';
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
    const file = form.get('video');
    if (file && typeof file !== 'string' && file.size > 0) {
      const cache = await caches.open(SHARE_CACHE);
      await cache.put('shared-video', new Response(file, {
        headers: { 'content-type': file.type || 'video/mp4', 'x-name': encodeURIComponent(file.name || 'video') },
      }));
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
