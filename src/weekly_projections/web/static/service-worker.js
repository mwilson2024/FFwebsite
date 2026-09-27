const CACHE = 'fantasy-hq-shell-v2';
const SHELL = ['/offline', '/static/favicon.svg', '/static/app-icon-192.png', '/static/app-icon-512.png'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).catch(() => caches.match('/offline')));
    return;
  }
  if (url.origin === self.location.origin && url.pathname.startsWith('/static/')) {
    event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request)));
  }
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(windows => {
    const destination = event.notification.data?.url || '/dashboard';
    const existing = windows[0];
    if (existing) return existing.focus().then(windowClient => windowClient.navigate(destination));
    return clients.openWindow(destination);
  }));
});

self.addEventListener('push', event => {
  let payload = {title:'Fantasy HQ update', body:'Open Fantasy HQ to see the latest roster alert.', url:'/dashboard', tag:'fantasy-hq-update'};
  try { if (event.data) payload = {...payload, ...event.data.json()}; } catch (_) {}
  const safeUrl = typeof payload.url === 'string' && payload.url.startsWith('/') && !payload.url.startsWith('//') ? payload.url : '/dashboard';
  event.waitUntil(self.registration.showNotification(payload.title, {
    body: payload.body, icon:'/static/app-icon-192.png', badge:'/static/app-icon-192.png',
    tag: payload.tag, renotify:true, data:{url:safeUrl},
  }));
});
