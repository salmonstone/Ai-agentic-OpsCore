import { useState } from 'react'
import { ago, postJSON, usePoll } from '../lib/api'
import { useDemo } from '../lib/demo'
import { tone } from '../lib/tone'
import { Icon, NoData, Pill, Section, SectionHead, SkeletonRows, TableCard } from './common'
import { logSources } from './LogViewer'

const kb = n => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`)

function ServiceCard({ icon, name, state, detail, sub, actions }) {
  const x = tone(state)
  return (
    <div className="surface" style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: 14, border: `1px ${x.bs} ${state === 'ok' ? 'var(--color-divider)' : x.line}` }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
        <Icon name={icon} size={18} style={{ color: 'var(--color-accent)' }} />
        <span style={{ fontSize: 14, fontWeight: 500, flex: 1 }}>{name}</span>
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11.5, fontWeight: 500, color: x.c }}><Icon name={x.icon} />{detail}</span>
      </div>
      <div className="muted" style={{ fontSize: 12 }}>{sub}</div>
      {actions && <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>{actions}</div>}
    </div>
  )
}

export default function System({ onLogs, onDaemon }) {
  const demo = useDemo()
  const sys = usePoll('/api/system', 15000)
  const logs = usePoll('/api/logs/system')
  const [busy, setBusy] = useState(null)
  const [msg, setMsg] = useState(null)
  const d = sys.data

  const act = async (key, fn) => {
    setBusy(key); setMsg(null)
    try { setMsg({ ok: true, text: await fn() }) } catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy(null); sys.reload()
  }
  const backupNow = () => act('backup', async () => { const r = await postJSON('/api/system/backup'); return `Backup ${r.name} created (${kb(r.size)}).` })
  const verify = name => act(`verify-${name}`, async () => {
    const r = await postJSON('/api/system/backup/verify', { name })
    if (!r.ok) throw new Error(`${name}: ${r.detail}`)
    return `${name} verified — every file and database checks out.`
  })
  const retry = id => act(`retry-${id}`, async () => { await postJSON(`/api/system/events/${id}/retry`); return 'Event re-queued.' })

  if (!d && sys.loading) return <SkeletonRows rows={6} />
  if (!d) return <NoData label="System" note="Couldn't read AtlasOS's status." error={sys.error} onRetry={() => sys.reload()} />

  const svc = d.supervisor.services || {}
  const q = d.queue
  const oldestMin = q?.oldest_pending ? Math.floor((Date.now() - Date.parse(q.oldest_pending)) / 60000) : null
  const daemonOn = d.daemon.running

  return (
    <div data-screen-label="System" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      {demo && <div className="muted" style={{ fontSize: 12.5 }}><Icon name="ph-info" /> The System page always shows your real AtlasOS — demo data doesn't apply here, and actions are disabled.</div>}
      {msg && <div style={{ fontSize: 12.5, color: msg.ok ? 'var(--st-ok)' : 'var(--st-crit)' }}><Icon name={msg.ok ? 'ph-check-circle' : 'ph-x-circle'} /> {msg.text}</div>}

      <Section>
        <SectionHead title="Services" note={d.supervisor.running ? `supervisor pid ${d.supervisor.pid} · updated ${ago(d.supervisor.updated)}` : null} />
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 10 }}>
          <ServiceCard icon="ph-shield-check" name="Supervisor" state={d.supervisor.running ? 'ok' : 'warn'}
            detail={d.supervisor.running ? 'running' : 'not running'}
            sub={d.supervisor.running ? 'Keeps the MCP server and ngrok up, restarting them if they die.' : 'MCP and ngrok aren\'t being watched. Start it with start-atlasos.bat (or it starts at logon).'}
            actions={<button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => onLogs(logSources.system('supervisor', 'Supervisor'))}><Icon name="ph-scroll" />Log</button>} />
          {['mcp', 'ngrok'].map(name => {
            const s = svc[name]
            const ok = s?.state === 'running' && s?.healthy
            return (
              <ServiceCard key={name} icon={name === 'mcp' ? 'ph-plugs-connected' : 'ph-globe'} name={name === 'mcp' ? 'MCP server' : 'ngrok tunnel'}
                state={!s ? 'unk' : ok ? 'ok' : 'crit'} detail={!s ? "couldn't check" : ok ? 'healthy' : s.state}
                sub={!s ? 'Only known while the supervisor runs.' : `${s.pid ? `pid ${s.pid} · ` : ''}up since ${ago(s.since)}${s.restarts ? ` · ${s.restarts} restart(s)` : ''}${s.reason ? ` · ${s.reason}` : ''}`}
                actions={<button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => onLogs(logSources.system(name, name === 'mcp' ? 'MCP server' : 'ngrok tunnel'))}><Icon name="ph-scroll" />Log</button>} />
            )
          })}
          <ServiceCard icon="ph-heartbeat" name="Healing daemon" state={daemonOn ? 'ok' : 'warn'} detail={daemonOn ? 'running' : 'stopped'}
            sub={daemonOn ? `pid ${d.daemon.pid} · ${d.daemon.alerts_sent_today} actions in 24h${d.daemon.last_check_time ? ` · last ${ago(d.daemon.last_check_time)}` : ''}` : 'Auto-healing is paused, and queued events (webhooks) wait until it runs.'}
            actions={<>
              <button className={`btn ${daemonOn ? 'btn-secondary' : 'btn-primary'}`} style={{ fontSize: 12 }} disabled={demo} onClick={onDaemon}><Icon name={daemonOn ? 'ph-stop' : 'ph-play'} />{daemonOn ? 'Stop' : 'Start'}</button>
              <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => onLogs(logSources.system('daemon', 'Daemon'))}><Icon name="ph-scroll" />Log</button>
            </>} />
          <ServiceCard icon="ph-squares-four" name="Dashboard" state="ok" detail="running"
            sub={`pid ${d.dashboard.pid} · started ${ago(d.dashboard.started)} · Python ${d.dashboard.python}`}
            actions={<button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => onLogs(logSources.system('dashboard', 'Dashboard server'))}><Icon name="ph-scroll" />Log</button>} />
        </div>
      </Section>

      <Section>
        <SectionHead title="Event queue" note="webhook events wait here for the daemon" />
        {q?.error ? <NoData note="Couldn't read the event queue." error={q.error} /> : (
          <>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 1, borderRadius: 'var(--radius-md)', overflow: 'hidden', background: 'var(--color-divider)', boxShadow: 'var(--shadow-sm)' }}>
              {['pending', 'processing', 'done', 'failed', 'dead'].map(k => (
                <div key={k} style={{ display: 'flex', flexDirection: 'column', gap: 2, padding: '10px 14px', background: 'var(--color-surface)' }}>
                  <span className="kicker" style={{ fontSize: 10.5 }}>{k}</span>
                  <span style={{ fontSize: 20, fontWeight: 600, fontVariantNumeric: 'tabular-nums', color: k === 'dead' && q.stats[k] ? 'var(--st-crit)' : undefined }}>{q.stats[k]}</span>
                </div>
              ))}
            </div>
            {q.stats.pending > 0 && !daemonOn && (
              <div style={{ fontSize: 12.5, color: 'var(--st-warn)' }}>
                <Icon name="ph-warning" /> {q.stats.pending} event(s) waiting{oldestMin != null ? `, oldest ${oldestMin < 60 ? `${oldestMin}m` : `${Math.floor(oldestMin / 60)}h`} old` : ''} — they're only processed while the daemon runs.
              </div>
            )}
            {q.dead?.length > 0 && (
              <TableCard>
                <table className="table">
                  <thead><tr><th>Dead event</th><th>Type</th><th>Error</th><th>Tries</th><th>Created</th><th /></tr></thead>
                  <tbody>
                    {q.dead.map(e => (
                      <tr key={e.id}>
                        <td className="mono muted" style={{ fontSize: 11.5 }}>{String(e.id).slice(0, 8)}</td>
                        <td className="mono" style={{ fontSize: 12 }}>{e.event_type}</td>
                        <td style={{ fontSize: 12, color: 'var(--st-crit)', maxWidth: 380 }}>{(e.error || '').slice(0, 160)}</td>
                        <td className="mono" style={{ fontSize: 12 }}>{e.retry_count}</td>
                        <td className="muted" style={{ fontSize: 12 }}>{ago(e.created_at)}</td>
                        <td style={{ textAlign: 'right' }}><button className="btn btn-ghost" style={{ fontSize: 12 }} disabled={demo || busy === `retry-${e.id}`} onClick={() => retry(e.id)}><Icon name="ph-arrow-clockwise" />Retry</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableCard>
            )}
          </>
        )}
      </Section>

      <Section>
        <SectionHead title="Backups" note={d.backups.dir ? `saved in ${d.backups.dir}` : null}>
          <button className="btn btn-primary" onClick={backupNow} disabled={demo || busy === 'backup'}>
            <Icon name={busy === 'backup' ? 'ph-circle-notch' : 'ph-archive'} className={busy === 'backup' ? 'spin' : undefined} />{busy === 'backup' ? 'Backing up…' : 'Back up now'}
          </button>
        </SectionHead>
        {d.backups.error && <NoData note="Couldn't list backups." error={d.backups.error} />}
        {!d.backups.error && d.backups.items.length === 0 && <div className="muted" style={{ fontSize: 12.5 }}>No backups yet.</div>}
        {d.backups.items.length > 0 && (
          <TableCard>
            <table className="table">
              <thead><tr><th>Backup</th><th>Created</th><th>Size</th><th>Type</th><th /></tr></thead>
              <tbody>
                {d.backups.items.map(b => (
                  <tr key={b.name}>
                    <td className="mono" style={{ fontSize: 12 }}>{b.name}</td>
                    <td className="muted" style={{ fontSize: 12 }}>{ago(b.created)}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{kb(b.size)}</td>
                    <td>{b.pre_restore ? <Pill t="neutral" label="before a restore" icon={false} /> : <Pill t="ok" label="regular" icon={false} />}</td>
                    <td style={{ textAlign: 'right' }}><button className="btn btn-ghost" style={{ fontSize: 12 }} disabled={busy === `verify-${b.name}`} onClick={() => verify(b.name)}><Icon name="ph-seal-check" />{busy === `verify-${b.name}` ? 'Verifying…' : 'Verify'}</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableCard>
        )}
        <div className="muted" style={{ fontSize: 11.5 }}>Restoring replaces AtlasOS's data while services are using it, so it stays a terminal step: stop AtlasOS, then <code>agent backup restore &lt;name&gt;</code>.</div>
      </Section>

      <Section>
        <SectionHead title="Logs" note="credentials are masked" />
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(210px, 1fr))', gap: 8 }}>
          {(logs.data?.logs || []).map(l => (
            <button key={l.name} className="hoverable" disabled={!l.exists} onClick={() => onLogs(logSources.system(l.name, l.label))}
              style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '10px 12px', borderRadius: 'var(--radius-md)', border: '1px solid var(--color-divider)', background: 'var(--color-surface)', textAlign: 'left', opacity: l.exists ? 1 : 0.5 }}>
              <Icon name="ph-scroll" size={17} style={{ color: 'var(--color-accent)' }} />
              <span style={{ flex: 1, fontSize: 13 }}>{l.label}</span>
              {!l.exists && <span className="muted" style={{ fontSize: 11 }}>no file yet</span>}
            </button>
          ))}
        </div>
      </Section>
    </div>
  )
}
