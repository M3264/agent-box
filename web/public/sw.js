/*
 * Agent Hub service worker — the away-from-browser half of notifications.
 *
 * Not bundled: it lives in web/public/ and Vite copies it verbatim to static/sw.js,
 * so this is plain browser JS with no imports and no build step. It does exactly two
 * things — show a notification when the server pushes one, and open the right job when
 * that notification is clicked.
 *
 * The payload the server sends is {title, body, hash, tag}. `hash` is a hash route
 * like `#/jobs/<id>`; resolving it against this worker's own registration scope is
 * what makes a click land under whichever mount the app was installed from (`/` or
 * `/hub/`) without the worker needing to know which.
 */

self.addEventListener('push', (event) => {
  let payload = {}
  try {
    payload = event.data ? event.data.json() : {}
  } catch (err) {
    payload = { body: event.data ? event.data.text() : '' }
  }
  const title = payload.title || 'Agent Hub'
  const options = {
    body: payload.body || '',
    // The in-tab announce() uses this same tag, so a push and an in-tab toast for one
    // event collapse into a single notification instead of double-buzzing a device
    // that also has a tab open.
    tag: payload.tag || 'agent-hub-attention',
    data: { hash: payload.hash || '#/attention' },
  }
  event.waitUntil(self.registration.showNotification(title, options))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const hash = (event.notification.data && event.notification.data.hash) || '#/attention'
  // registration.scope is the absolute URL the worker was registered under
  // (`https://host/` or `https://host/hub/`), so the mount prefix is already baked in.
  const target = new URL(self.registration.scope)
  target.hash = hash
  const href = target.href

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      // Focus an Agent Hub tab that is already open and steer it to the job, rather
      // than opening a second window on top of it.
      for (const client of clients) {
        if ('focus' in client) {
          if ('navigate' in client) {
            client.navigate(href).catch(() => undefined)
          }
          return client.focus()
        }
      }
      return self.clients.openWindow(href)
    }),
  )
})
