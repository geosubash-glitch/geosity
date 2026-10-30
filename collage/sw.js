// Moodboard service worker.
// - Keeps the page and its scripts on the phone so it opens fast.
// - Receives links shared from Instagram & co. (Web Share Target) and hands
//   them to the page, which sends them on to GitHub.
// The board itself (../board/) always comes fresh from the network.
// Cache names all start with "reel-collage-" so this never touches the rest of
// the site's caches. Another worker on this site may clear ours; everything
// here re-downloads on demand, so that only costs a refetch.

const CACHE = 'reel-collage-v3';
const SHELL = ['./', './index.html', './app.js', './layout.js', './manifest.webmanifest', './icon-192.png', './apple-touch-icon.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('reel-collage-') && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function receiveShare(request) {
  const scope = self.registration.scope;
  let link = '';
  let hasFile = false;
  try {
    const form = await request.formData();
    const text = ['url', 'text', 'title'].map((k) => form.get(k) || '').join(' ');
    link = (text.match(/https?:\/\/[^\s<>"')\]]+/i) || [''])[0];
    hasFile = form.getAll('video').some((f) => f && typeof f !== 'string' && f.size > 0);
  } catch {
    // fall through
  }
  const target = new URL('./', scope);
  if (link) target.searchParams.set('link', link);
  else if (hasFile) target.searchParams.set('video', '1');
  return Response.redirect(target.href, 303);
}

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;

  if (e.request.method === 'POST' && url.pathname.endsWith('/share')) {
    e.respondWith(receiveShare(e.request));
    return;
  }
  if (e.request.method !== 'GET' || e.request.headers.has('range')) return;
  if (url.pathname.includes('/board/') && url.pathname.endsWith('.json')) return; // always live

  if (/\.(png|jpg|bin)$/.test(url.pathname)) {
    // Images never change once published: cache first.
    e.respondWith(
      caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
        return res;
      }))
    );
    return;
  }

  // The page and its scripts: network first so updates arrive, cache when offline.
  e.respondWith(
    fetch(e.request).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
      return res;
    }).catch(() => caches.match(e.request).then((hit) => hit || caches.match('./index.html')))
  );
});
