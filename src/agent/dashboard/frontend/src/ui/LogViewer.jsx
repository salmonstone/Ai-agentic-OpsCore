import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getJSON } from '../lib/api'
import { useDemo } from '../lib/demo'
import { Icon } from './common'

// Where each kind of log comes from. Every source returns tabs of plain text.
export const logSources = {
  pod: (namespace, pod) => ({
    title: pod, sub: `pod · ${namespace}`, icon: 'ph-cube',
    load: async () => {
      const r = await getJSON(`/api/logs/pod?namespace=${encodeURIComponent(namespace)}&pod=${encodeURIComponent(pod)}&lines=500`)
      return [{ id: 'logs', label: 'Logs', text: r.logs }, { id: 'events', label: 'Events', text: r.events }]
    },
    ask: `Read the logs of pod ${pod} in namespace ${namespace} and tell me what's wrong.`,
  }),
  jenkins: (job, build) => ({
    title: `${job} #${build}`, sub: 'Jenkins build log', icon: 'ph-hammer',
    load: async () => {
      const r = await getJSON(`/api/logs/jenkins?job=${encodeURIComponent(job)}&build=${build}&lines=1500`)
      return [{ id: 'logs', label: 'Console', text: r.logs }]
    },
    ask: `Diagnose Jenkins build ${job} #${build}. What's the root cause and can it be fixed automatically?`,
  }),
  github: (repo, runId, workflow) => ({
    title: `${workflow || 'run'} · ${runId}`, sub: `GitHub Actions · ${repo} · failed jobs`, icon: 'ph-github-logo',
    load: async () => {
      const { jobs = [] } = await getJSON(`/api/github/jobs?repo=${encodeURIComponent(repo)}&run_id=${runId}`)
      if (!jobs.length) return [{ id: 'none', label: 'Logs', text: '', empty: 'No failed jobs in this run.' }]
      return Promise.all(jobs.slice(0, 6).map(async j => {
        const r = await getJSON(`/api/github/job-log?repo=${encodeURIComponent(repo)}&job_id=${j.id}`)
        const head = j.failed_steps?.length ? `# failed step(s): ${j.failed_steps.join(', ')}\n` : ''
        return { id: String(j.id), label: j.name, text: head + (r.logs || '') }
      }))
    },
    ask: `Diagnose GitHub Actions run ${runId} in ${repo}. What failed and how do I fix it?`,
  }),
  system: (name, label) => ({
    title: label, sub: 'AtlasOS log', icon: 'ph-scroll',
    load: async () => {
      const r = await getJSON(`/api/logs/system?name=${encodeURIComponent(name)}&lines=1500`)
      return [{ id: 'logs', label: 'Log', text: r.missing ? '' : r.text, empty: r.missing ? "This log file doesn't exist yet — the service hasn't written anything." : null }]
    },
  }),
}

const DEMO_LOG = [
  '2026-10-01T09:41:02Z INFO  starting api v2.14.1 (commit 8c1f2e0)',
  '2026-10-01T09:41:02Z INFO  loading config from /etc/api/config.yaml',
  '2026-10-01T09:41:03Z INFO  connecting to postgres at db.prod.svc:5432',
  '2026-10-01T09:41:03Z ERROR pq: password authentication failed for user "api"',
  '2026-10-01T09:41:03Z WARN  retrying database connection (1/3)',
  '2026-10-01T09:41:05Z ERROR pq: password authentication failed for user "api"',
  '2026-10-01T09:41:09Z FATAL could not connect to database after 3 attempts — exiting',
].join('\n')

function lineTone(l) {
  if (/\b(ERROR|FATAL|CRITICAL|Exception|Traceback|FAILED|failed)\b/.test(l)) return 'var(--st-crit)'
  if (/\b(WARN|WARNING|Warning|BackOff)\b/.test(l)) return 'var(--st-warn)'
  if (/\b(DEBUG|TRACE)\b/.test(l)) return 'var(--muted)'
  return undefined
}

export default function LogViewer({ source, onClose, onAsk }) {
  const demo = useDemo()
  const [tabs, setTabs] = useState(null)
  const [tab, setTab] = useState(0)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [q, setQ] = useState('')
  const [wrap, setWrap] = useState(true)
  const [follow, setFollow] = useState(false)
  const [copied, setCopied] = useState(false)
  const bodyRef = useRef(null)

  const load = useCallback(async () => {
    if (!source) return
    setLoading(true)
    try {
      setTabs(demo ? [{ id: 'logs', label: 'Logs', text: DEMO_LOG }] : await source.load())
      setError(null)
    } catch (e) {
      setError(e.message)
    }
    setLoading(false)
  }, [source, demo])

  useEffect(() => { setTabs(null); setTab(0); setQ(''); setError(null); setFollow(false); load() }, [source]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!follow) return undefined
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [follow, load])
  useEffect(() => { if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight }, [tabs, tab])
  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const current = tabs?.[tab]
  const lines = useMemo(() => (current?.text || '').split('\n'), [current])
  const needle = q.trim().toLowerCase()
  const shown = needle ? lines.filter(l => l.toLowerCase().includes(needle)) : lines

  if (!source) return null
  const copy = async () => {
    try { await navigator.clipboard.writeText(shown.join('\n')); setCopied(true); setTimeout(() => setCopied(false), 1500) } catch { /* blocked */ }
  }

  return (
    <>
      <div onClick={onClose} style={{ position: 'fixed', inset: 0, zIndex: 50, background: 'color-mix(in srgb, var(--color-bg) 60%, transparent)' }} />
      <aside role="dialog" aria-label={`Logs: ${source.title}`} style={{ position: 'fixed', top: 0, right: 0, bottom: 0, zIndex: 51, width: 'min(860px, 100vw)', display: 'flex', flexDirection: 'column', background: 'var(--color-surface)', boxShadow: 'var(--shadow-lg)' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, padding: '14px 16px 10px', background: 'var(--rule) no-repeat bottom / 100% 1px' }}>
          <Icon name={source.icon} size={20} style={{ color: 'var(--color-accent)', marginTop: 2 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="mono" style={{ fontSize: 15, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{source.title}</div>
            <div className="muted" style={{ fontSize: 12 }}>{source.sub}{demo ? ' · demo sample' : ''}</div>
          </div>
          <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close"><Icon name="ph-x" size={16} /></button>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', padding: '10px 16px' }}>
          {tabs && tabs.length > 1 && (
            <div role="tablist" style={{ display: 'inline-flex', border: '1px solid var(--color-divider)', borderRadius: 'var(--radius-md)', overflow: 'hidden' }}>
              {tabs.map((t, i) => (
                <button key={t.id} role="tab" aria-selected={tab === i} onClick={() => setTab(i)} style={{ padding: '5px 12px', border: 0, borderLeft: i ? '1px solid var(--color-divider)' : 0, background: 'transparent', fontSize: 12.5, cursor: 'pointer', color: tab === i ? 'var(--color-accent)' : 'var(--muted)', boxShadow: tab === i ? 'inset 0 0 0 1px var(--color-accent)' : 'none' }}>{t.label}</button>
              ))}
            </div>
          )}
          <div style={{ position: 'relative', flex: '1 1 200px' }}>
            <Icon name="ph-funnel" style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--muted)' }} />
            <input id="log-filter" className="input mono" value={q} onChange={e => setQ(e.target.value)} placeholder="Filter lines (e.g. error)" style={{ paddingLeft: 30, fontSize: 12.5 }} />
          </div>
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, cursor: 'pointer' }}>
            <input type="checkbox" id="log-follow" checked={follow} onChange={e => setFollow(e.target.checked)} disabled={demo} />Follow
          </label>
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, cursor: 'pointer' }}>
            <input type="checkbox" id="log-wrap" checked={wrap} onChange={e => setWrap(e.target.checked)} />Wrap
          </label>
          <button className="btn btn-secondary" onClick={load} disabled={loading} style={{ padding: '5px 10px' }}>
            <Icon name="ph-arrow-clockwise" className={loading ? 'spin' : undefined} />Reload
          </button>
        </div>

        <div ref={bodyRef} className="mono" style={{ flex: 1, overflow: 'auto', margin: '0 16px', padding: '10px 12px', borderRadius: 'var(--radius-md)', background: 'var(--term)', fontSize: 12, lineHeight: 1.6 }}>
          {!tabs && !error && <div className="muted"><Icon name="ph-circle-notch" className="spin" /> Loading…</div>}
          {error && (
            <div style={{ color: 'var(--st-unk)', display: 'flex', gap: 6 }}>
              <Icon name="ph-question" style={{ marginTop: 3 }} /><span>Couldn't load this log — {error}</span>
            </div>
          )}
          {current && !current.text && <div className="muted">{current.empty || 'Empty.'}</div>}
          {current?.text && shown.map((l, i) => (
            <div key={i} style={{ whiteSpace: wrap ? 'pre-wrap' : 'pre', wordBreak: wrap ? 'break-word' : 'normal', color: lineTone(l) }}>{l || ' '}</div>
          ))}
          {current?.text && needle && shown.length === 0 && <div className="muted">No lines match “{q}”.</div>}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 16px 14px' }}>
          <span className="muted mono" style={{ fontSize: 11 }}>{current?.text ? `${shown.length}${needle ? ` of ${lines.length}` : ''} lines` : ''}</span>
          <button className="btn btn-ghost" onClick={copy} disabled={!current?.text} style={{ marginLeft: 'auto' }}><Icon name={copied ? 'ph-check' : 'ph-copy'} />{copied ? 'Copied' : 'Copy'}</button>
          {source.ask && <button className="btn btn-primary" onClick={() => { onClose(); onAsk(source.ask) }}><Icon name="ph-sparkle" />Ask the assistant</button>}
        </div>
      </aside>
    </>
  )
}
