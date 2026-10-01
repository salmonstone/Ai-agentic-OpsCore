// Desktop (browser) notifications: one preference shared by the bell menu
// and the Settings page. A change in one place is announced to the other.
const KEY = 'atlas-desktop-notify'
const EVENT = 'atlas-notify-pref'

export const notifySupported = () => 'Notification' in window

export function notifyEnabled() {
  try { return localStorage.getItem(KEY) === '1' && notifySupported() && Notification.permission === 'granted' } catch { return false }
}

/** Turn desktop alerts on (asks the browser for permission) or off.
 *  Resolves to the new state; 'denied' if the browser refused. */
export async function setNotify(on) {
  if (on) {
    if (!notifySupported()) return false
    const perm = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission()
    if (perm !== 'granted') return 'denied'
  }
  try { localStorage.setItem(KEY, on ? '1' : '0') } catch { /* private window */ }
  window.dispatchEvent(new Event(EVENT))
  return on
}

export function onNotifyChange(fn) {
  window.addEventListener(EVENT, fn)
  return () => window.removeEventListener(EVENT, fn)
}

export function showNotification(title, body, tag) {
  try { new Notification(`AtlasOS · ${title}`, { body: (body || '').slice(0, 180), tag }) } catch { /* unsupported */ }
}
