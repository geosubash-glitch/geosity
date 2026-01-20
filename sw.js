const CACHE_NAME = 'retro-radio-v2';
const ASSETS = [
    './index.html',
    './manifest.json',
    './images/background.gif',
    './images/icon-192.png',
    './images/icon-512.png',
    './images/fire_bullet.gif',
    './images/under_construction.gif',
    './images/construction_bar.gif',
    './images/email_me.gif',
    './picmix.com_68043.gif'
];

self.addEventListener('install', (e) => {
    e.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
    );
});

self.addEventListener('fetch', (e) => {
    e.respondWith(
        caches.match(e.request).then((response) => response || fetch(e.request))
    );
});
