import { useEffect, useRef, useState } from 'react'
import { ago, postJSON } from '../lib/api'
import { useData, useDemo } from '../lib/demo'
import { notifyEnabled, notifySupported, onNotifyChange, setNotify, showNotification } from '../lib/notify'
import { Icon } from './common'

const SEV = {
  critical: ['var(--st-crit)', 'ph-x-circle'], warning: ['var(--st-warn)', 'ph-warning'],
  info: ['var(--color-accent)', 'ph-info'], ok: ['var(--st-ok)', 'ph-check-circle'],
}
const KIND_ICON = { approval: 'ph-seal-check', summary: 'ph-note', page: 'ph-siren', incident: 'ph-siren', resolved: 'ph-check-circle' }

// Where a click on a notification takes you.
function target(n) {
  if (n.kind === 'approval') return 'approvals'
  if (n.kind === 'incident' || n.kind === 'page') return 'incidents'
  if (n.kind === 'summary') return 'overview'
  if (n.meta?.Namespace || n.meta?.Resource) return 'cluster'
  if (/jenkins/i.test(n.title)) return 'jenkins'
  return null
}

export default function Inbox({ onNav, demo }) {
  const demoCtx = useDemo()
  const isDemo = demo ?? demoCtx
  const inbox = useData('notifications', '/api/notifications?limit=60', 15000, isDemo)
  const [open, setOpen] = useState(false)
  const [desktop, setDesktop] = useState(notifyEnabled)
  const seen = useRef(null)
  const ref = useRef(null)

  const items = inbox.data?.items || []
  const unread = inbox.data?.unread || 0

  // Desktop notification for anything new and urgent since the last poll.
  useEffect(() => {
    if (!inbox.data || isDemo) return
    const ids = new Set(items.map(i => i.id))
    if (seen.current && desktop && notifyEnabled()) {
      for (const n of items) {
        if (!seen.current.has(n.id) && !n.read && (n.severity === 'critical' || n.severity === 'warning' || n.kind === 'approval' || n.kind === 'incident')) {
          showNotification(n.title, n.message, n.id)
        }
      }
    }
    seen.current = ids
  }, [inbox.data]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => onNotifyChange(() => setDesktop(notifyEnabled())), [])

  useEffect(() => {
    if (!open) return undefined
    const onDown = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    const onKey = e => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('mousedown', onDown); window.addEventListener('keydown', onKey)
    return () => { window.removeEventListener('mousedown', onDown); window.removeEventListener('keydown', onKey) }
  }, [open])

  const markAll = async () => { if (!isDemo) { await postJSON('/api/notifications/read', {}).catch(() => {}); inbox.reload() } }
  const openItem = async n => {
    if (!isDemo && !n.read) { await postJSON('/api/notifications/read', { ids: [n.id] }).catch(() => {}); inbox.reload() }
    const t = target(n)
    if (t) { onNav(t); setOpen(false) }
  }
  const toggleDesktop = async () => { await setNotify(!desktop); setDesktop(notifyEnabled()) }

  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <button className="btn btn-secondary" onClick={() => setOpen(o => !o)} aria-label={`Notifications${unread ? `, ${unread} unread` : ''}`}
        style={{ position: 'relative', padding: '5px 9px' }}>
        <Icon name="ph-bell" size={16} />
        {unread > 0 && (
          <span className="mono" style={{ position: 'absolute', top: -6, right: -6, minWidth: 18, height: 18, padding: '0 5px', borderRadius: 9, display: 'grid', placeItems: 'center', fontSize: 10.5, fontWeight: 600, color: '#fff', background: 'var(--st-crit)', boxShadow: '0 0 0 2px var(--color-bg)' }}>
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>
      {open && (
        <div role="dialog" aria-label="Notifications" style={{ position: 'absolute', right: 0, top: 'calc(100% + 8px)', zIndex: 35, width: 'min(420px, calc(100vw - 32px))', maxHeight: '70vh', display: 'flex', flexDirection: 'column', borderRadius: 'var(--radius-lg)', background: 'var(--color-surface)', boxShadow: 'var(--shadow-lg)', overflow: 'hidden' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 14px', background: 'var(--rule) no-repeat bottom / 100% 1px' }}>
            <span style={{ fontSize: 14, fontWeight: 500, flex: 1 }}>Notifications{unread ? ` · ${unread} new` : ''}</span>
            <button className="btn btn-ghost" onClick={markAll} disabled={!unread} style={{ fontSize: 12 }}>Mark all read</button>
          </div>
          <div style={{ overflow: 'auto', flex: 1 }}>
            {inbox.error && <div style={{ padding: 14, fontSize: 12.5, color: 'var(--st-unk)' }}><Icon name="ph-question" /> Couldn't load notifications: {inbox.error}</div>}
            {inbox.data && items.length === 0 && (
              <div className="muted" style={{ padding: '28px 16px', textAlign: 'center', fontSize: 12.5 }}>
                <Icon name="ph-bell-slash" size={22} /><div style={{ marginTop: 6 }}>No alerts yet. Everything AtlasOS sends — to Slack or not — shows up here.</div>
              </div>
            )}
            {items.map(n => {
              const [c, sevIcon] = SEV[n.severity] || SEV.info
              const t = target(n)
              return (
                <button key={n.id} onClick={() => openItem(n)} className="rowbtn" style={{
                  display: 'flex', gap: 10, width: '100%', padding: '10px 14px', border: 0, textAlign: 'left', cursor: t || !n.read ? 'pointer' : 'default',
                  background: n.read ? 'transparent' : 'color-mix(in srgb, var(--color-accent) 7%, transparent)',
                  borderBottom: '1px solid color-mix(in srgb, var(--color-text) 6%, transparent)',
                }}>
                  <Icon name={KIND_ICON[n.kind] || sevIcon} size={17} style={{ color: c, marginTop: 1, flex: 'none' }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: n.read ? 400 : 600, lineHeight: 1.35 }}>{n.title}</div>
                    {n.message && <div className="muted" style={{ fontSize: 12, marginTop: 2, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{n.message.replace(/[*`]/g, '')}</div>}
                    <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>{ago(n.created_at)} · {n.kind}{t ? ` · open ${t}` : ''}</div>
                  </div>
                  {!n.read && <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--color-accent)', marginTop: 6, flex: 'none' }} />}
                </button>
              )
            })}
          </div>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px', fontSize: 12, background: 'var(--rule) no-repeat top / 100% 1px', cursor: 'pointer' }}>
            <input type="checkbox" id="desktop-notify" checked={desktop} onChange={toggleDesktop} disabled={isDemo || !notifySupported()} />
            Desktop alerts for critical, warning and approval notifications
          </label>
        </div>
      )}
    </div>
  )
}
