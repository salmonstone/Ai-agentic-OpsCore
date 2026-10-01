import { useEffect, useState } from 'react'
import { ago, postJSON } from '../lib/api'
import { useDemo } from '../lib/demo'
import { Icon, NoData, OkEmpty, Pill, SkeletonRows, TableCard } from './common'

const OUT = {
  applied: ['Applied', 'ok', 'ph-check-circle'], failed: ['Failed', 'crit', 'ph-x-circle'],
  approved: ['Approved', 'accent', 'ph-seal-check'], rejected: ['Rejected', 'neutral', 'ph-prohibit'],
  expired: ['Expired', 'neutral', 'ph-hourglass'],
}
const OutPill = ({ s }) => { const o = OUT[s] || [s, 'neutral']; return <Pill t={o[1]} label={o[0]} icon={o[2]} /> }

const mmss = ms => { const s = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` }

function Card({ kicker, kickerIcon, summary, why, params, children }) {
  return (
    <article className="surface" style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 210px', gap: '16px 20px', padding: 16 }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
        <div className="muted" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: 11.5 }}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '1px 8px', borderRadius: 6, border: '1px solid var(--color-divider)', color: 'var(--color-text)' }}>
            <Icon name={kickerIcon} />{kicker}
          </span>
          {why}
        </div>
        <div style={{ fontSize: 16, fontWeight: 500, lineHeight: 1.3 }}>{summary}</div>
        {params && <pre className="term" style={{ margin: 0, padding: '9px 12px', fontSize: 11.5, lineHeight: 1.55 }}>{params}</pre>}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, justifyContent: 'flex-end' }}>{children}</div>
    </article>
  )
}

function Decide({ busy, done, onApprove, onReject, approveLabel = 'Approve & apply' }) {
  const demo = useDemo()
  if (demo) return (
    <>
      <button className="btn btn-danger" disabled><Icon name="ph-check" />{approveLabel}</button>
      <button className="btn btn-secondary" disabled><Icon name="ph-x" />Reject</button>
      <span className="muted" style={{ fontSize: 11.5 }}>Disabled in demo mode.</span>
    </>
  )
  if (busy) return <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: 'var(--color-accent)' }}><Icon name="ph-circle-notch" className="spin" />Applying…</div>
  if (done) return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <OutPill s={done.status} />
      {done.message && <span className="muted" style={{ fontSize: 12 }}>{done.message}</span>}
    </div>
  )
  return (
    <>
      <button className="btn btn-danger" onClick={onApprove}><Icon name="ph-check" />{approveLabel}</button>
      <button className="btn btn-secondary" onClick={onReject}><Icon name="ph-x" />Reject</button>
    </>
  )
}

export default function Approvals({ approvals, deploys, onChanged }) {
  const [tab, setTab] = useState('pending')
  const [busy, setBusy] = useState({})
  const [done, setDone] = useState({})
  const [now, setNow] = useState(Date.now())
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t) }, [])

  const decide = async (id, approve) => {
    setBusy(b => ({ ...b, [id]: true }))
    try {
      const r = await postJSON(`/api/approvals/${id}/decide`, { approve })
      setDone(d => ({ ...d, [id]: r }))
    } catch (e) {
      setDone(d => ({ ...d, [id]: { status: 'failed', message: e.message } }))
    }
    setBusy(b => ({ ...b, [id]: false }))
    onChanged()
  }
  const decideDeploy = async (id, approve) => {
    setBusy(b => ({ ...b, [id]: true }))
    try {
      const r = await postJSON(`/api/deploys/${id}/${approve ? 'approve' : 'reject'}`)
      setDone(d => ({ ...d, [id]: { status: r.status === 'approved' ? 'applied' : r.status === 'gated' ? 'failed' : 'rejected',
        message: r.status === 'gated' ? 'Blocked by the deploy health gate.' : (r.log || []).slice(-1)[0] || '' } }))
    } catch (e) {
      setDone(d => ({ ...d, [id]: { status: 'failed', message: e.message } }))
    }
    setBusy(b => ({ ...b, [id]: false }))
    onChanged()
  }

  const d = approvals.data
  // keep cards the user just decided visible (with their outcome) until they leave the page
  const pending = d ? [...d.pending, ...d.history.filter(h => done[h.id])] : []
  const pendingDeploys = deploys.data || []
  const count = (d?.pending.length || 0) + pendingDeploys.length

  return (
    <div data-screen-label="04 Approvals" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div role="tablist" style={{ display: 'inline-flex', alignSelf: 'flex-start', border: '1px solid var(--color-divider)', borderRadius: 'var(--radius-md)', overflow: 'hidden' }}>
        {[['pending', `Pending · ${count}`], ['history', 'History']].map(([id, label], i) => (
          <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)} style={{
            padding: '7px 14px', border: 0, borderLeft: i ? '1px solid var(--color-divider)' : 0, background: 'transparent', fontSize: 12.5, cursor: 'pointer',
            color: tab === id ? 'var(--color-accent)' : 'var(--muted)', boxShadow: tab === id ? 'inset 0 0 0 1px var(--color-accent)' : 'none',
          }}>{label}</button>
        ))}
      </div>

      {!d && approvals.loading && <SkeletonRows rows={3} />}
      {!d && approvals.error && <NoData label="Approvals" note={'Not "nothing waiting" — the approvals store couldn\'t be read.'} error={approvals.error} onRetry={() => approvals.reload()} />}

      {d && tab === 'pending' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {pending.length === 0 && pendingDeploys.length === 0 && (
            <OkEmpty title="Nothing waiting for you" sub="Risky fixes land here and in Slack. Approving here runs the same path as the Slack button." />
          )}
          {pending.map(a => {
            const rem = Date.parse(a.expires_at) - now
            const total = Date.parse(a.expires_at) - Date.parse(a.created_at)
            const low = rem < 5 * 60000
            return (
              <Card key={a.id} kicker={a.kind} kickerIcon="ph-wrench" summary={a.summary}
                why={<><span>requested {ago(a.created_at)}</span><span>·</span><span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><Icon name="ph-slack-logo" />also in Slack</span></>}
                params={Object.entries(a.params || {}).map(([k, v]) => `${k.padEnd(14)} ${typeof v === 'object' ? JSON.stringify(v) : v}`).join('\n')}>
                {!done[a.id] && !busy[a.id] && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
                      <span className="kicker" style={{ fontSize: 10.5 }}>Expires in</span>
                      <span className="mono" style={{ fontSize: 20, color: low ? 'var(--st-warn)' : 'var(--color-text)' }}>{mmss(rem)}</span>
                    </div>
                    <div style={{ height: 3, borderRadius: 2, background: 'color-mix(in srgb, var(--color-text) 9%, transparent)', overflow: 'hidden' }}>
                      <div style={{ height: '100%', width: `${Math.max(0, Math.min(100, (rem / total) * 100))}%`, background: low ? 'var(--st-warn)' : 'var(--color-accent)', transition: 'width 1s linear' }} />
                    </div>
                  </div>
                )}
                <Decide busy={busy[a.id]} done={done[a.id]} onApprove={() => decide(a.id, true)} onReject={() => decide(a.id, false)} />
              </Card>
            )
          })}
          {pendingDeploys.map(p => (
            <Card key={p.id} kicker="Deploy" kickerIcon="ph-rocket-launch" summary={`Deploy ${p.new_image} to ${p.namespace}/${p.service}`}
              why={<span>requested {ago(p.requested_at)}</span>}
              params={[p.reason, `repo      ${p.repo} (${p.branch})`, `from      ${p.old_image || '—'}`, `to        ${p.new_image}`].filter(Boolean).join('\n')}>
              <Decide busy={busy[p.id]} done={done[p.id]} approveLabel="Approve & deploy" onApprove={() => decideDeploy(p.id, true)} onReject={() => decideDeploy(p.id, false)} />
            </Card>
          ))}
        </div>
      )}

      {d && tab === 'history' && (
        d.history.length === 0
          ? <div className="muted" style={{ fontSize: 12.5 }}>No decisions yet.</div>
          : (
            <TableCard>
              <table className="table">
                <thead><tr><th>When</th><th>Fix</th><th>Kind</th><th>Outcome</th><th>By</th><th>Detail</th></tr></thead>
                <tbody>
                  {d.history.map(h => (
                    <tr key={h.id}>
                      <td className="muted" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>{ago(h.decided_at || h.created_at)}</td>
                      <td>{h.summary}</td>
                      <td className="mono" style={{ fontSize: 11.5 }}>{h.kind}</td>
                      <td><OutPill s={h.status} /></td>
                      <td className="muted" style={{ fontSize: 12 }}>{h.decided_by || '—'}</td>
                      <td className="muted" style={{ fontSize: 12 }}>{h.result?.message || ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableCard>
          )
      )}
    </div>
  )
}
