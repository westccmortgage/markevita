/* Receives push even without an open Studio tab. Never caches application data. */
self.addEventListener('push', event => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil(self.registration.showNotification(data.title || 'MarkeVita', {
    body: data.body || '', tag: data.tag || 'studio', data: {url: data.url},
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const scope = new URL(self.registration.scope);
  const url = new URL(event.notification.data?.url || scope.href, scope);
  if (url.origin !== scope.origin || !url.pathname.startsWith(scope.pathname)) return;
  event.waitUntil(clients.openWindow(url.href));
});
