const CACHE = 'examflow-v1';
const APP_FILES = ['/static/style.css', '/static/claymorphism.css', '/static/layout-fix.css', '/static/app.js', '/static/manifest.webmanifest', '/static/examflow-icon.svg'];
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(APP_FILES))));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request)));
});
