// Minimal service worker: makes the app installable and tolerable on flaky
// connections. Static assets (own + CDN) are cached cache-first; pages go
// network-first with the cache as offline fallback. POSTs are never touched.
const CACHE = 'cms-v1';

self.addEventListener('install', function () {
    self.skipWaiting();
});

self.addEventListener('activate', function (event) {
    event.waitUntil(
        caches.keys()
            .then(function (keys) {
                return Promise.all(keys.filter(function (k) { return k !== CACHE; })
                    .map(function (k) { return caches.delete(k); }));
            })
            .then(function () { return self.clients.claim(); })
    );
});

self.addEventListener('fetch', function (event) {
    const request = event.request;
    if (request.method !== 'GET') return;
    const url = new URL(request.url);

    // Never cache dynamic media or the admin.
    if (url.pathname.startsWith('/media/') || url.pathname.startsWith('/admin/')) return;

    if (url.pathname.startsWith('/static/') || url.origin !== self.location.origin) {
        // Assets: cache-first (they are versioned/immutable enough).
        event.respondWith(
            caches.open(CACHE).then(function (cache) {
                return cache.match(request).then(function (hit) {
                    return hit || fetch(request).then(function (response) {
                        if (response.ok) cache.put(request, response.clone());
                        return response;
                    });
                });
            })
        );
        return;
    }

    // Pages: network-first, fall back to the last cached copy when offline.
    event.respondWith(
        fetch(request)
            .then(function (response) {
                if (response.ok) {
                    const copy = response.clone();
                    caches.open(CACHE).then(function (cache) { cache.put(request, copy); });
                }
                return response;
            })
            .catch(function () { return caches.match(request); })
    );
});
