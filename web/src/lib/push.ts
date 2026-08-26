/**
 * Turning this browser's Web Push subscription on and off.
 *
 * `announce()` in `useAttention` only fires while an Agent Hub tab is open — close
 * every tab and nothing reaches you. This is the other half: a service worker the
 * browser keeps alive in the background, plus a subscription the server can POST an
 * encrypted push to. The server holds a VAPID keypair and fans out to every stored
 * subscription; here we register the worker, subscribe with the server's public key,
 * and hand the subscription back for storage.
 *
 * Everything is per-browser and opt-in: nothing here runs until the operator ticks
 * the switch, and the permission prompt must follow that click.
 */

import { api } from '../api'

/** Whether this browser can do Web Push at all, so Settings can explain a "no". */
export function pushSupported(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    'serviceWorker' in navigator &&
    typeof window !== 'undefined' &&
    'PushManager' in window &&
    'Notification' in window
  )
}

/**
 * Subscribe this browser and store it server-side.
 *
 * Assumes Notification permission is already granted — the caller gates that on the
 * click, because the prompt only appears after a gesture. Returns a short reason
 * string on the failures worth explaining (push off server-side, or an incomplete
 * subscription), or null on success. Throws only on genuinely unexpected errors,
 * which the caller surfaces.
 */
export async function enablePush(): Promise<string | null> {
  if (!pushSupported()) return 'This browser cannot receive push notifications.'

  const info = await api.push()
  if (!info.configured || !info.key) {
    return 'Push is switched off on the server, so nothing can be delivered.'
  }

  // Relative registration, so the worker's scope is the mount the document was served
  // from (`/` or `/hub/`) — the same reason the router is hash-based and assets are
  // referenced relatively.
  const registration = await navigator.serviceWorker.register('sw.js')
  await navigator.serviceWorker.ready

  let subscription = await registration.pushManager.getSubscription()
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({
      // Chrome refuses a subscription that is not user-visible, which suits us exactly:
      // every push here is meant to show a notification.
      userVisibleOnly: true,
      applicationServerKey: urlB64ToUint8Array(info.key),
    })
  }

  // `toJSON()` gives the endpoint plus the base64url-encoded keys the server needs,
  // rather than reaching into `getKey()` and encoding them by hand.
  const json = subscription.toJSON()
  const p256dh = json.keys?.p256dh
  const auth = json.keys?.auth
  if (!json.endpoint || !p256dh || !auth) {
    return 'The browser produced an incomplete subscription; nothing was stored.'
  }
  await api.pushSubscribe({ endpoint: json.endpoint, keys: { p256dh, auth } })
  return null
}

/** Unsubscribe this browser and drop its row on the server. Idempotent. */
export async function disablePush(): Promise<void> {
  if (!pushSupported()) return
  const registration = await navigator.serviceWorker.getRegistration()
  const subscription = await registration?.pushManager.getSubscription()
  if (!subscription) return
  const { endpoint } = subscription
  try {
    await subscription.unsubscribe()
  } finally {
    // Drop the row even if the browser-side unsubscribe threw: a stale row only earns
    // one dead send before the server prunes it, but leaving it is still noise.
    await api.pushUnsubscribe(endpoint)
  }
}

/**
 * Decode a base64url VAPID key into the `Uint8Array` `subscribe()` expects. The
 * standard helper — browsers accept the byte array, not the string.
 */
function urlB64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const normalised = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = window.atob(normalised)
  const output = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i)
  return output
}
