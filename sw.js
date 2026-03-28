/* ═══════════════════════════════════════════════
   LaceBit AI — Service Worker (V6 Final Fix)
═══════════════════════════════════════════════ */
const CACHE_NAME = 'lacebit-v6'; // Forced update

const PRECACHE = [
  '/static/bot.png',
  '/manifest.json'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(PRECACHE))
    .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

/* ── NATIVE PUSH EVENT ── */
self.addEventListener('push', function(event) {
    let data = { title: 'Class Alert', body: 'Time for your next lecture!' };
    try { if (event.data) { data = event.data.json(); } } catch (e) {}
    const options = {
        body: data.body,
        icon: '/static/bot.png',
        badge: '/static/bot.png',
        vibrate: [200, 100, 200],
        data: { url: '/home' }
    };
    event.waitUntil(self.registration.showNotification(data.title, options));
});

/* ── FETCH LOGIC ── */
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // 1. CRITICAL BYPASS: Routes that must NEVER be cached or handled by SW
  // Including /login and /register here prevents the "Form Resubmission" cache bug
  const bypassRoutes = ['/chat', '/reminders', '/attendance', '/api/save-subscription', '/login', '/logout', '/register'];
  
  if (bypassRoutes.some(path => url.pathname.startsWith(path))) {
    // Force browser to handle this request normally
    return; 
  }

  // 2. Skip non-GET
  if (event.request.method !== 'GET' || !url.protocol.startsWith('http')) return;

  // 3. Static Assets: Cache First
  if (url.pathname.startsWith('/static')) {
    event.respondWith(
      caches.match(event.request).then(cached => {
        return cached || fetch(event.request).then(res => {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
          return res;
        });
      })
    );
    return;
  }

  // 4. HTML Pages & Others: Network First
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});