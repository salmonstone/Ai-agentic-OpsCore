import { useEffect, useState } from 'react'
import { ago, getJSON } from '../lib/api'
import { DEMO_ACTIVITY, useDemo } from '../lib/demo'
import { Icon, NoData, Section, SectionHead, SkeletonRows } from './common'

const SOURCE_ICON = {
  dashboard: 'ph-squares-four', approvals: 'ph-seal-check', 'auto-healer': 'ph-heartbeat', 'node-healer': 'ph-heartbeat',
  'k8s-fix': 'ph-wrench', 'security-fix': 'ph-shield-check', 'tls-fix': 'ph-lock-key', 'cost-aws-fix': 'ph-currency-dollar',
  'terraform-fix': 'ph-tree-structure', deployment: 'ph-rocket-launch', backup: 'ph-archive', secrets: 'ph-key',
  jenkins: 'ph-hammer', 'multi-cluster-add': 'ph-cube',
}

function dayLabel(iso) {
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`)
  const today = new Date(); const y = new Date(Date.now() - 86400000)
  if (d.toDateString() === today.toDateString()) return 'Today'
  if (d.toDateString() === y.toDateString()) return 'Yesterday'
  return d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'short' })
}

function Entry({ m }) {
  const [open, setOpen] = useState(false)
  const lines = m.content.split('\n')
  const long = lines.length > 3 || m.content.length > 300
  const ts = m.created_at.endsWith('Z') || m.created_at.includes('+') ? m.created_at : `${m.created_at}Z`
  return (
    <div style={{ display: 'flex', gap: 10, padding: '10px 0', background: 'var(--rule) no-repeat bottom / 100% 1px' }}>
      <Icon name={SOURCE_ICON[m.source] || 'ph-note'} size={17} style={{ color: 'var(--color-accent)', marginTop: 2, flex: 'none' }} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 12.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word', ...(open || !long ? {} : { display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden' }) }}>
          {m.content}
        </div>
        <div className="muted" style={{ fontSize: 11, marginTop: 3, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <span title={new Date(ts).toLocaleString()}>{ago(ts)}</span><span className="mono">{m.source}</span>
          {long && <button className="btn btn-ghost" style={{ fontSize: 11, padding: 0 }} onClick={() => setOpen(o => !o)}>{open ? 'Show less' : 'Show all'}</button>}
          {m.truncated && open && <span>(cut at 4,000 characters)</span>}
        </div>
      </div>
    </div>
  )
}

export default function Activity() {
  const demo = useDemo()
  const [view, setView] = useState('actions')
  const [source, setSource] = useState('')
  const [q, setQ] = useState('')
  const [query, setQuery] = useState('')
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [loading, setLoading] = useState(false)
  const [more, setMore] = useState(true)

  useEffect(() => { const t = setTimeout(() => setQuery(q.trim()), 300); return () => clearTimeout(t) }, [q])

  const url = before => `/api/activity?view=${view}&source=${encodeURIComponent(source)}&q=${encodeURIComponent(query)}&limit=50${before ? `&before=${encodeURIComponent(before)}` : ''}`
  useEffect(() => {
    if (demo) { setData(DEMO_ACTIVITY()); setMore(false); return }
    let alive = true
    setLoading(true); setErr(null)
    getJSON(url()).then(r => { if (alive) { setData(r); setMore(r.items.length === 50) } })
      .catch(e => { if (alive) setErr(e.message) }).finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [view, source, query, demo]) // eslint-disable-line react-hooks/exhaustive-deps

  const loadMore = async () => {
    const last = data.items[data.items.length - 1]
    setLoading(true)
    try { const r = await getJSON(url(last.created_at)); setData(d => ({ ...d, items: [...d.items, ...r.items] })); setMore(r.items.length === 50) } catch (e) { setErr(e.message) }
    setLoading(false)
  }

  const sources = Object.entries(data?.sources || {}).filter(([s]) => view === 'all' || (data?.action_sources || []).includes(s))
  let lastDay = null

  return (
    <div data-screen-label="Activity" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <div role="tablist" style={{ display: 'inline-flex', borderRadius: 'var(--radius-md)', border: '1px solid var(--color-divider)', overflow: 'hidden' }}>
          {[['actions', 'Things done'], ['all', 'Everything']].map(([v, label]) => (
            <button key={v} role="tab" aria-selected={view === v} onClick={() => { setView(v); setSource('') }}
              style={{ padding: '6px 12px', border: 0, fontSize: 12.5, cursor: 'pointer', background: view === v ? 'color-mix(in srgb, var(--color-accent) 16%, transparent)' : 'transparent', color: 'var(--color-text)' }}>{label}</button>
          ))}
        </div>
        <select className="input" aria-label="Source" value={source} onChange={e => setSource(e.target.value)} style={{ fontSize: 12.5, width: 'auto' }}>
          <option value="">{view === 'actions' ? 'All actions' : 'All sources'}</option>
          {sources.map(([s, n]) => <option key={s} value={s}>{s} ({n})</option>)}
        </select>
        <div style={{ flex: '1 1 220px', display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="ph-magnifying-glass" style={{ color: 'var(--muted)' }} />
          <input className="input" aria-label="Search activity" placeholder="Search — a pod, a job, a person…" value={q} onChange={e => setQ(e.target.value)} style={{ flex: 1, fontSize: 12.5 }} />
        </div>
      </div>
      <div className="muted" style={{ fontSize: 12 }}>
        {view === 'actions' ? 'What AtlasOS and people changed: fixes, approvals, deploys, backups, settings and everything done from this dashboard.' : 'Every record, including scans and diagnoses.'}
        {' '}This is AtlasOS's audit trail — the same records the CLI writes.
      </div>

      <Section>
        {!data && loading && <SkeletonRows rows={6} />}
        {err && <NoData note="Couldn't read the audit trail." error={err} />}
        {data && data.items.length === 0 && <div className="muted" style={{ fontSize: 12.5 }}>{query ? 'Nothing matches.' : 'Nothing recorded yet.'}</div>}
        {data && data.items.length > 0 && (
          <div className="surface" style={{ padding: '2px 16px' }}>
            {data.items.map(m => {
              const day = dayLabel(m.created_at)
              const head = day !== lastDay ? (lastDay = day) : null
              return (
                <div key={m.id}>
                  {head && <div style={{ paddingTop: 12 }}><SectionHead title={head} /></div>}
                  <Entry m={m} />
                </div>
              )
            })}
          </div>
        )}
        {data && more && data.items.length > 0 && <div><button className="btn btn-secondary" onClick={loadMore} disabled={loading}>{loading ? 'Loading…' : 'Load older'}</button></div>}
      </Section>
    </div>
  )
}
