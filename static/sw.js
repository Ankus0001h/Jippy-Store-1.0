// Jippy Store Admin Notification & Web Push Service Worker
// Enables native desktop & mobile alerts even when browser tabs are closed

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

// 🔔 Listen for incoming Web Push & FCM messages (Works when browser tab is closed)
self.addEventListener('push', (event) => {
  let payload = {
    title: '🛒 New Order Received | Jippy Store',
    body: 'A new customer order has been placed on Jippy Store.',
    icon: '/static/logo.jpeg',
    badge: '/static/logo.jpeg',
    data: {
      orderId: null,
      url: '/admin/orders'
    }
  };

  if (event.data) {
    try {
      const json = event.data.json();
      if (json.notification) {
        payload.title = json.notification.title || payload.title;
        payload.body = json.notification.body || payload.body;
        if (json.notification.icon) payload.icon = json.notification.icon;
      } else {
        payload.title = json.title || payload.title;
        payload.body = json.body || payload.body;
        if (json.icon) payload.icon = json.icon;
      }
      if (json.data) {
        payload.data = { ...payload.data, ...json.data };
      }
    } catch (e) {
      payload.body = event.data.text() || payload.body;
    }
  }

  const notificationOptions = {
    body: payload.body,
    icon: payload.icon || '/static/logo.jpeg',
    badge: payload.badge || '/static/logo.jpeg',
    vibrate: [200, 100, 200, 100, 200],
    tag: payload.data.orderId ? `order-${payload.data.orderId}` : 'jippy-order-alert',
    renotify: true,
    requireInteraction: true, // Stays visible until user clicks
    data: payload.data,
    actions: [
      { action: 'open_order', title: '👀 View Order' },
      { action: 'dismiss', title: '✕ Dismiss' }
    ]
  };

  event.waitUntil(
    self.registration.showNotification(payload.title, notificationOptions)
  );
});

// 👆 Handle notification click: focus or open the admin orders tab
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  if (event.action === 'dismiss') {
    return;
  }

  const orderId = event.notification.data ? event.notification.data.orderId : null;
  const targetUrl = (event.notification.data && event.notification.data.url) ? event.notification.data.url : '/admin/orders';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if (client.url.includes('/admin/orders') && 'focus' in client) {
          if (orderId) {
            client.postMessage({ type: 'FOCUS_ORDER', orderId: orderId });
          }
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});

// 🔄 Auto-renew push subscription if token rotated
self.addEventListener('pushsubscriptionchange', (event) => {
  if (event.oldSubscription && event.oldSubscription.options) {
    event.waitUntil(
      self.registration.pushManager.subscribe(event.oldSubscription.options)
        .then((subscription) => {
          return fetch('/admin/api/register-push-token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ subscription: subscription.toJSON() })
          });
        })
    );
  }
});
