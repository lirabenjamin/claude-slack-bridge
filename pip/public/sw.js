// Minimal network-first service worker. Cache only the app shell.
const CACHE = 'pip-v1'
const SHELL = ['/', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png']

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))),
    ).then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url)
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(fetch(e.request))
    return
  }
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        if (e.request.method === 'GET' && r.ok) {
          const copy = r.clone()
          caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {})
        }
        return r
      })
      .catch(() => caches.match(e.request).then((c) => c || caches.match('/'))),
  )
})
