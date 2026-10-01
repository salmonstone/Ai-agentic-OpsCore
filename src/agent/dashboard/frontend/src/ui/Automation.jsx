import { useState } from 'react'
import { ago, getJSON, postJSON } from '../lib/api'
import { useData, useDemo } from '../lib/demo'
import { sectionTone, tone } from '../lib/tone'
import { Card, ConfirmButton, Field, Icon, Msg, NoData, Pill, Section, SectionHead, SkeletonRows, TableCard } from './common'
import { logSources } from './LogViewer'

const RUN_T = { success: 'ok', partial: 'warn', failed: 'crit', running: 'neutral' }
const STEP_ICON = { kubectl: 'ph-terminal', slack: 'ph-slack-logo', python: 'ph-code' }
const VAR_LABEL = { pod_name: 'Pod', namespace: 'Namespace', node_name: 'Node', pvc_name: 'PVC', deployment_name: 'Deployment', usage_pct: 'Usage %' }

// ── runbooks ──────────────────────────────────────────────────────────────────
function RunbookCard({ rb, onRan }) {
  const demo = useDemo()
  const [open, setOpen] = useState(false)
  const [ctx, setCtx] = useState({ namespace: 'default' })
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [err, setErr] = useState(null)
  const run = async () => {
    setBusy(true); setErr(null); setResult(null)
    try { setResult(await postJSON(`/api/runbooks/${rb.id}/run`, { context: ctx })); onRan() } catch (e) { setErr(e.message) }
    setBusy(false)
  }
  return (
    <Card>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
        <Icon name="ph-book-open" size={19} style={{ color: 'var(--color-accent)', marginTop: 2 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 500 }}>{rb.name}</div>
          <div className="muted" style={{ fontSize: 12 }}>{rb.description}</div>
          {rb.trigger_condition && <div className="mono muted" style={{ fontSize: 11, marginTop: 3 }}>daemon runs it when: {rb.trigger_condition}</div>}
        </div>
        <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => setOpen(o => !o)}>{open ? 'Close' : 'Steps & run'}</button>
      </div>
      {open && (
        <>
          <ol style={{ margin: 0, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {rb.steps.map((s, i) => (
              <li key={i} style={{ fontSize: 12.5 }}>
                <Icon name={STEP_ICON[s.type] || 'ph-dot-outline'} style={{ color: 'var(--muted)' }} /> <span style={{ fontWeight: 500 }}>{s.name}</span>
                <span className="mono muted" style={{ fontSize: 11.5 }}> {s.detail}</span>
                {s.on_failure === 'continue' && <span className="muted" style={{ fontSize: 11 }}> · keeps going if it fails</span>}
              </li>
            ))}
          </ol>
          {rb.vars.length > 0 && (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(170px, 1fr))', gap: 8 }}>
              {rb.vars.map(v => (
                <Field key={v} id={`rb-${rb.id}-${v}`} label={VAR_LABEL[v] || v}>
                  <input id={`rb-${rb.id}-${v}`} className="input mono" value={ctx[v] || ''} onChange={e => setCtx(c => ({ ...c, [v]: e.target.value.trim() }))} placeholder={v} />
                </Field>
              ))}
            </div>
          )}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <ConfirmButton onConfirm={run} busy={busy} disabled={demo} confirmLabel="Run it on the cluster?">Run runbook</ConfirmButton>
            <span className="muted" style={{ fontSize: 11.5 }}>Runs these steps for real, against the current cluster.</span>
          </div>
          {err && <Msg msg={{ ok: false, text: err }} />}
          {result && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div><Pill t={RUN_T[result.status] || 'neutral'} label={`Runbook ${result.status || 'finished'}`} /></div>
              {result.error && <Msg msg={{ ok: false, text: result.error }} />}
              {(result.steps || []).map((s, i) => (
                <div key={i} style={{ fontSize: 12, display: 'flex', gap: 6 }}>
                  <Icon name={s.status === 'success' ? 'ph-check' : 'ph-x'} style={{ color: s.status === 'success' ? 'var(--st-ok)' : 'var(--st-crit)', marginTop: 2 }} />
                  <span style={{ fontWeight: 500 }}>{s.step}</span>
                  {s.output && <span className="mono muted" style={{ fontSize: 11, wordBreak: 'break-word' }}>{String(s.output).slice(0, 300)}</span>}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Card>
  )
}

function RunHistory({ runs }) {
  const [open, setOpen] = useState(null)
  const [steps, setSteps] = useState({})
  const toggle = async id => {
    setOpen(o => (o === id ? null : id))
    if (!steps[id]) { try { const r = await getJSON(`/api/runbooks/runs/${id}`); setSteps(s => ({ ...s, [id]: r.steps })) } catch { /* */ } }
  }
  if (!runs.length) return <div className="muted" style={{ fontSize: 12.5 }}>No runs yet — from here, or by the daemon when a trigger matches.</div>
  return (
    <TableCard>
      <table className="table">
        <thead><tr><th>When</th><th>Runbook</th><th>Trigger</th><th>Steps</th><th>Result</th><th /></tr></thead>
        <tbody>
          {runs.map(r => [
            <tr key={r.id}>
              <td className="muted" style={{ fontSize: 12 }}>{ago(r.started_at)}</td>
              <td className="mono" style={{ fontSize: 12 }}>{r.runbook_id}</td>
              <td style={{ fontSize: 12 }}>{r.trigger}</td>
              <td className="mono" style={{ fontSize: 12 }}>{r.steps_done ?? '—'}/{r.steps_total}</td>
              <td><Pill t={RUN_T[r.status] || 'neutral'} label={r.status} icon={false} /></td>
              <td style={{ textAlign: 'right' }}><button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => toggle(r.id)}>{open === r.id ? 'Hide' : 'Steps'}</button></td>
            </tr>,
            open === r.id && (
              <tr key={`${r.id}-s`}><td colSpan={6}>
                {!steps[r.id] ? <span className="muted" style={{ fontSize: 12 }}>Loading…</span> : steps[r.id].map((s, i) => (
                  <div key={i} style={{ fontSize: 12, display: 'flex', gap: 6, padding: '2px 0' }}>
                    <Icon name={s.status === 'success' ? 'ph-check' : s.status === 'failed' ? 'ph-x' : 'ph-dot-outline'} style={{ color: s.status === 'success' ? 'var(--st-ok)' : s.status === 'failed' ? 'var(--st-crit)' : 'var(--muted)', marginTop: 2 }} />
                    <span style={{ fontWeight: 500 }}>{s.step_name}</span>
                    <span className="mono muted" style={{ fontSize: 11, wordBreak: 'break-word' }}>{String(s.error || s.output || '').slice(0, 300)}</span>
                  </div>
                ))}
              </td></tr>
            ),
          ])}
        </tbody>
      </table>
    </TableCard>
  )
}

// ── scaling ──────────────────────────────────────────────────────────────────
const SCALE_EMPTY = { deployment: '', namespace: 'default', down_time: '17:30', up_time: '03:30', down_replicas: 1, up_replicas: 3, cpu_pct: 80 }

function utcToLocal(hhmm) {
  if (!hhmm) return ''
  const [h, m] = hhmm.split(':').map(Number)
  const d = new Date(); d.setUTCHours(h, m, 0, 0)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function Scaling({ scale }) {
  const demo = useDemo()
  const [form, setForm] = useState(SCALE_EMPTY)
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(null)
  const d = scale.data
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))
  const act = async (key, fn) => {
    setBusy(key); setMsg(null)
    try { setMsg({ ok: true, text: await fn() }) } catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy(null); scale.reload()
  }
  const add = e => {
    e.preventDefault()
    act('add', async () => {
      await postJSON('/api/scale', { ...form, down_replicas: +form.down_replicas, up_replicas: +form.up_replicas, cpu_pct: +form.cpu_pct })
      setForm(SCALE_EMPTY)
      return `Policy saved for ${form.deployment}.${d?.daemon_running ? '' : ' The daemon applies schedules — start it on the System page.'}`
    })
  }
  const toggle = p => act(`t-${p.id}`, async () => { await postJSON(`/api/scale/${p.id}/${p.enabled ? 'disable' : 'enable'}`); return `${p.deployment}: ${p.enabled ? 'paused' : 'resumed'}.` })

  if (!d && scale.loading) return <SkeletonRows rows={3} />
  if (!d) return <NoData note="Couldn't read scaling policies." error={scale.error} onRetry={() => scale.reload()} />
  return (
    <>
      {!d.daemon_running && d.policies.some(p => p.enabled) && (
        <div style={{ fontSize: 12.5, color: 'var(--st-warn)' }}><Icon name="ph-warning" /> The healing daemon is stopped, so no schedule below is being applied.</div>
      )}
      <Msg msg={msg} />
      {d.policies.length > 0 && (
        <TableCard>
          <table className="table">
            <thead><tr><th>Deployment</th><th>Scale down</th><th>Scale up</th><th>CPU scale-up</th><th>Status</th><th /></tr></thead>
            <tbody>
              {d.policies.map(p => (
                <tr key={p.id} style={{ opacity: p.enabled ? 1 : 0.55 }}>
                  <td className="mono" style={{ fontSize: 12 }}>{p.deployment}<span className="muted">/{p.namespace}</span></td>
                  <td style={{ fontSize: 12 }}>{p.schedule_down_utc ? <>to <b>{p.down_replicas}</b> at {p.schedule_down_utc} UTC <span className="muted">({utcToLocal(p.schedule_down_utc)} here)</span></> : '—'}</td>
                  <td style={{ fontSize: 12 }}>{p.schedule_up_utc ? <>to <b>{p.up_replicas}</b> at {p.schedule_up_utc} UTC <span className="muted">({utcToLocal(p.schedule_up_utc)} here)</span></> : '—'}</td>
                  <td className="mono" style={{ fontSize: 12 }}>&gt; {p.cpu_threshold_pct}% · max {p.max_replicas}</td>
                  <td>{p.enabled ? <Pill t="ok" label="active" icon={false} /> : <Pill t="neutral" label="paused" icon={false} />}</td>
                  <td style={{ textAlign: 'right' }}><button className="btn btn-ghost" style={{ fontSize: 12 }} disabled={demo || busy === `t-${p.id}`} onClick={() => toggle(p)}><Icon name={p.enabled ? 'ph-pause' : 'ph-play'} />{p.enabled ? 'Pause' : 'Resume'}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableCard>
      )}
      <Card>
        <form onSubmit={add} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div style={{ fontSize: 13.5, fontWeight: 500 }}>Add a schedule <span className="muted" style={{ fontWeight: 400, fontSize: 12 }}>— e.g. shrink non-prod overnight to save money</span></div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 10 }}>
            <Field id="s-dep" label="Deployment"><input id="s-dep" className="input mono" required placeholder="api" value={form.deployment} onChange={e => set('deployment', e.target.value.trim())} /></Field>
            <Field id="s-ns" label="Namespace"><input id="s-ns" className="input mono" required value={form.namespace} onChange={e => set('namespace', e.target.value.trim())} /></Field>
            <Field id="s-dt" label="Scale down at (UTC)" hint={`${utcToLocal(form.down_time)} your time`}><input id="s-dt" className="input mono" type="time" required value={form.down_time} onChange={e => set('down_time', e.target.value)} /></Field>
            <Field id="s-dr" label="…to replicas"><input id="s-dr" className="input mono" type="number" min="0" max="50" required value={form.down_replicas} onChange={e => set('down_replicas', e.target.value)} /></Field>
            <Field id="s-ut" label="Scale up at (UTC)" hint={`${utcToLocal(form.up_time)} your time`}><input id="s-ut" className="input mono" type="time" required value={form.up_time} onChange={e => set('up_time', e.target.value)} /></Field>
            <Field id="s-ur" label="…to replicas"><input id="s-ur" className="input mono" type="number" min="1" max="50" required value={form.up_replicas} onChange={e => set('up_replicas', e.target.value)} /></Field>
            <Field id="s-cpu" label="Add replicas above CPU %"><input id="s-cpu" className="input mono" type="number" min="10" max="100" required value={form.cpu_pct} onChange={e => set('cpu_pct', e.target.value)} /></Field>
          </div>
          <div><button className="btn btn-primary" type="submit" disabled={demo || busy === 'add'}><Icon name="ph-plus" />Add policy</button></div>
        </form>
      </Card>
      {d.events.length > 0 && (
        <TableCard>
          <table className="table">
            <thead><tr><th>When</th><th>Deployment</th><th>Change</th><th>Why</th><th>Result</th></tr></thead>
            <tbody>
              {d.events.map(e => (
                <tr key={e.id}>
                  <td className="muted" style={{ fontSize: 12 }}>{ago(e.created_at)}</td>
                  <td className="mono" style={{ fontSize: 12 }}>{e.deployment}<span className="muted">/{e.namespace}</span></td>
                  <td className="mono" style={{ fontSize: 12 }}>{e.old_replicas} → {e.new_replicas}</td>
                  <td style={{ fontSize: 12 }}>{e.reason || e.action}</td>
                  <td><Pill t={e.status === 'done' ? 'ok' : e.status === 'failed' ? 'crit' : 'neutral'} label={e.status} icon={false} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableCard>
      )}
    </>
  )
}

// ── daily summary ────────────────────────────────────────────────────────────
function DailySummary({ summary, onLogs }) {
  const demo = useDemo()
  const info = useData('summary_info', '/api/summary/info', 120000)
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)
  const send = async force => {
    setBusy(true); setMsg(null)
    try {
      const r = await postJSON('/api/summary/send', { force })
      setMsg(r.sent ? { ok: true, text: `Sent to Slack — ${r.headline}` } : { ok: false, text: r.reason === 'already sent today' ? "Already sent today — use Send again to post another." : r.reason })
      info.reload()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy(false)
  }
  const sch = info.data?.schedule
  const s = summary.data
  return (
    <>
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <Icon name="ph-sun-horizon" size={20} style={{ color: 'var(--color-accent)' }} />
          <div style={{ flex: '1 1 260px', minWidth: 0 }}>
            <div style={{ fontSize: 14, fontWeight: 500 }}>Daily Slack summary</div>
            <div className="muted" style={{ fontSize: 12 }}>
              {!sch ? 'Checking the schedule…' : sch.available
                ? <>Scheduled ({sch.schedule || 'daily'}) — next {sch.next_run || '—'}{sch.last_run && !/N\/A|1999/.test(sch.last_run) ? ` · last ran ${sch.last_run}` : ''}{sch.status ? ` · ${sch.status}` : ''}</>
                : sch.detail}
              {info.data && !info.data.slack && <span style={{ color: 'var(--st-warn)' }}> · Slack isn't connected, so nothing can be sent.</span>}
            </div>
          </div>
          <button className="btn btn-primary" onClick={() => send(false)} disabled={demo || busy || info.data?.slack === false}><Icon name={busy ? 'ph-circle-notch' : 'ph-paper-plane-tilt'} className={busy ? 'spin' : undefined} />Send today's</button>
          <button className="btn btn-secondary" onClick={() => send(true)} disabled={demo || busy || info.data?.slack === false} title="Post again even if today's was already sent">Send again</button>
          <button className="btn btn-ghost" onClick={() => onLogs(logSources.system('summary', 'Daily summary (last run)'))}><Icon name="ph-scroll" />Last run log</button>
        </div>
        <Msg msg={msg} />
      </Card>
      {s && (
        <Card>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 13.5, fontWeight: 500 }}>Today's summary preview</span>
            <span className="muted" style={{ fontSize: 12 }}>{s.headline} · built {ago(s.generated_at)}</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 10 }}>
            {s.sections.map(sec => {
              const t = tone(sectionTone(sec.status))
              return (
                <div key={sec.title} style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12.5 }}>
                  <div style={{ display: 'flex', gap: 6, alignItems: 'center', fontWeight: 500 }}><Icon name={t.icon} style={{ color: t.c }} />{sec.title}</div>
                  {sec.lines.slice(0, 4).map((l, i) => <div key={i} className="muted" style={{ fontSize: 12, paddingLeft: 20 }}>{l.replace(/[`*]/g, '').replace(/^\s*❌\s*/, '')}</div>)}
                </div>
              )
            })}
          </div>
        </Card>
      )}
      {info.data?.history?.length > 0 && (
        <div className="muted" style={{ fontSize: 12 }}>
          Sent: {info.data.history.map(h => new Date(h.created_at).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })).join(' · ')}
        </div>
      )}
    </>
  )
}

export default function Automation({ summary, onLogs }) {
  const catalog = useData('runbooks', '/api/runbooks/catalog', 30000)
  const scale = useData('scale', '/api/scale', 30000)
  const c = catalog.data
  return (
    <div data-screen-label="Automation" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      <Section>
        <SectionHead title="Runbooks" note={c ? `${c.runbooks.length} playbooks · edit them in ${c.file}` : null} />
        {!c && catalog.loading && <SkeletonRows rows={3} />}
        {!c && catalog.error && <NoData note="Couldn't read the runbooks." error={catalog.error} onRetry={() => catalog.reload()} />}
        {c && c.runbooks.length === 0 && <div className="muted" style={{ fontSize: 12.5 }}>No runbooks defined in {c.file}.</div>}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(380px, 1fr))', gap: 10, alignItems: 'start' }}>
          {c?.runbooks.map(rb => <RunbookCard key={rb.id} rb={rb} onRan={() => catalog.reload()} />)}
        </div>
        {c && <RunHistory runs={c.runs} />}
      </Section>

      <Section>
        <SectionHead title="Auto-scaling" note="scheduled scale-down/up and CPU scale-up, applied by the daemon" />
        <Scaling scale={scale} />
      </Section>

      <Section>
        <SectionHead title="Daily summary" note="one Slack message a day — its absence means AtlasOS itself is down" />
        <DailySummary summary={summary} onLogs={onLogs} />
      </Section>
    </div>
  )
}
