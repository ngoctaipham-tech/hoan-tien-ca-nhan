const CACHE = 'hoantien-v3';
const ASSETS = ['./', './index.html', './manifest.webmanifest',
  './icons/apple-touch-icon.png', './icons/icon-192.png', './icons/icon-512.png', './icons/icon-maskable-512.png',
  './data/cards.json', './data/promos.json', './data/update_status.json'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
function networkFirst(req, key) {
  return fetch(req, { cache: 'no-store' }).then(r => {
    if (r.ok) { const cp = r.clone(); caches.open(CACHE).then(c => c.put(key || req, cp)); }
    return r;
  }).catch(() => caches.match(key || req, { ignoreSearch: true }));
}
self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  // Trang chính & dữ liệu ưu đãi (data/*.json): ưu tiên mạng, mất mạng thì dùng bản đã lưu
  if (req.mode === 'navigate') { e.respondWith(networkFirst(req, './index.html')); return; }
  if (/\/data\/[^/]+\.json$/.test(url.pathname)) {
    const key = new Request(url.origin + url.pathname);
    e.respondWith(networkFirst(key, key)); return;
  }
  e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(r => {
    if (r.ok) { const cp = r.clone(); caches.open(CACHE).then(c => c.put(req, cp)); }
    return r;
  })));
});
