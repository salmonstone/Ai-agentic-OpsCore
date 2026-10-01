import { useEffect, useMemo, useRef, useState } from 'react'
import { runCommand } from '../lib/api'
import { useDemo } from '../lib/demo'
import { lineColor } from '../lib/tone'
import { Icon, NoData, SkeletonRows } from './common'

const isToggle = t => t === 'bool' || t === 'flag'

function Toggle({ on, onFlip }) {
  return (
    <button role="switch" aria-checked={on} onClick={onFlip} style={{ display: 'inline-flex', alignItems: 'center', gap: 9, minHeight: 34, padding: '0 2px', border: 0, background: 'transparent', cursor: 'pointer', fontSize: 12.5, alignSelf: 'flex-start' }}>
      <span style={{ position: 'relative', width: 32, height: 18, borderRadius: 9, background: on ? 'color-mix(in srgb, var(--color-accent) 40%, transparent)' : 'color-mix(in srgb, var(--color-text) 12%, transparent)', border: `1px solid ${on ? 'var(--color-accent)' : 'var(--color-divider)'}`, transition: 'background .15s' }}>
        <span style={{ position: 'absolute', top: 2, left: on ? 16 : 2, width: 12, height: 12, borderRadius: '50%', background: on ? 'var(--color-accent)' : 'var(--muted)', transition: 'left .15s' }} />
      </span>
      {on ? 'on' : 'off'}
    </button>
  )
}

export default function Commands({ commands, context, initial }) {
  const demo = useDemo()
  const [q, setQ] = useState('')
  const [sel, setSel] = useState(initial || null)
  const [vals, setVals] = useState({})
  const [confirm, setConfirm] = useState(false)
  const [lines, setLines] = useState(null)
  const [status, setStatus] = useState('idle')  // idle | running | ok | failed
  const outRef = useRef(null)

  const all = useMemo(() => Object.values(commands.data || {}).flat().filter(c => !c.is_eval), [commands.data])
  const cmd = all.find(c => c.full_command === sel) || null
  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const g = {}
    for (const c of all) {
      if (needle && !`${c.full_command} ${c.help_text}`.toLowerCase().includes(needle)) continue
      ;(g[c.group] ||= []).push(c)
    }
    return Object.entries(g)
  }, [all, q])

  useEffect(() => { setVals({}); setConfirm(false); setLines(null); setStatus('idle') }, [sel])
  useEffect(() => { if (outRef.current) outRef.current.scrollTop = outRef.current.scrollHeight }, [lines])

  if (!commands.data && commands.loading) return <SkeletonRows rows={8} />
  if (!commands.data) return <NoData label="Commands" note="Couldn't load the command list from the dashboard server." error={commands.error} onRetry={() => commands.reload()} />

  const value = p => (vals[p.name] !== undefined ? vals[p.name] : (p.default ?? (isToggle(p.type) ? false : '')))
  const cmdLine = cmd ? ['agent', cmd.full_command, ...cmd.params.flatMap(p => {
    const v = value(p)
    if (isToggle(p.type)) return v ? [`--${p.name.replace(/_/g, '-')}`] : []
    return v === '' || v == null ? [] : [`--${p.name.replace(/_/g, '-')}`, String(v)]
  })].join(' ') : ''

  const run = async () => {
    if (!cmd) return
    const params = {}
    for (const p of cmd.params) {
      const v = value(p)
      if (isToggle(p.type)) { if (v) params[p.name] = true }
      else if (v !== '' && v != null) params[p.name] = p.type === 'int' ? parseInt(v, 10) : p.type === 'float' ? parseFloat(v) : v
    }
    setLines([`$ ${cmdLine}`]); setStatus('running')
    try {
      const code = await runCommand(cmd.full_command, params, cmd.is_destructive ? confirm : false, ln => setLines(l => [...(l || []), ln]))
      setStatus(code === 0 ? 'ok' : 'failed')
    } catch (e) {
      setLines(l => [...(l || []), `✗ ${e.message}`]); setStatus('failed')
    }
    setConfirm(false)
  }

  const running = status === 'running'
  const ST = { idle: ['idle', 'var(--muted)'], running: ['running…', 'var(--color-accent)'], ok: ['exit 0', 'var(--st-ok)'], failed: ['failed', 'var(--st-crit)'] }[status]

  return (
    <div data-screen-label="06 Command Runner" style={{ display: 'grid', gridTemplateColumns: '250px minmax(0,1fr)', gap: 14, alignItems: 'start' }}>
      <div className="surface" style={{ position: 'sticky', top: 18, display: 'flex', flexDirection: 'column', gap: 8, padding: 8, maxHeight: 'calc(100vh - 120px)', overflow: 'auto' }}>
        <div style={{ position: 'relative' }}>
          <Icon name="ph-magnifying-glass" style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--muted)' }} />
          <input className="input" id="cmd-search" value={q} onChange={e => setQ(e.target.value)} placeholder={`Search ${all.length} commands`} style={{ paddingLeft: 30 }} />
        </div>
        {groups.map(([g, items]) => (
          <div key={g} style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            <div className="kicker" style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 8px 3px', fontSize: 10.5 }}><span>{g}</span><span>{items.length}</span></div>
            {items.map(c => {
              const cur = c.full_command === sel
              return (
                <button key={c.full_command} className={cur ? '' : 'rowbtn'} onClick={() => setSel(c.full_command)} style={{
                  display: 'flex', alignItems: 'center', gap: 8, width: '100%', padding: '5px 8px', border: 0, borderRadius: 6, textAlign: 'left', cursor: 'pointer',
                  fontFamily: 'var(--font-mono)', fontSize: 12,
                  background: cur ? 'color-mix(in srgb, var(--color-accent) 16%, transparent)' : 'transparent', color: cur ? 'var(--color-text)' : 'color-mix(in srgb, var(--color-text) 82%, transparent)',
                }}>
                  <span style={{ flex: 1 }}>{c.full_command}</span>
                  {c.is_destructive && <span title="changes things" style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--st-crit)' }} />}
                </button>
              )
            })}
          </div>
        ))}
        {groups.length === 0 && <div className="muted" style={{ padding: '10px 8px', fontSize: 12 }}>No commands match “{q}”.</div>}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>
        {!cmd && (
          <div className="surface" style={{ padding: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 500 }}>Pick a command</div>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>
              Every <code>agent</code> CLI command is listed here, read from the CLI itself — add a command to cli.py and it shows up.
              A red dot means it changes something; those need a confirm tick before Run.
            </div>
          </div>
        )}
        {cmd && (
          <div className="surface" style={{ display: 'flex', flexDirection: 'column', gap: 14, padding: 16 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <span className="mono" style={{ fontSize: 17, fontWeight: 500 }}>{cmd.full_command}</span>
                {cmd.is_destructive && (
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, padding: '1px 8px', borderRadius: 6, fontSize: 11, fontWeight: 500, color: 'var(--st-crit)', background: 'color-mix(in srgb, var(--st-crit) 14%, transparent)', border: '1px solid color-mix(in srgb, var(--st-crit) 40%, transparent)' }}>
                    <Icon name="ph-warning-octagon" />changes things
                  </span>
                )}
                <span className="muted" style={{ fontSize: 11, marginLeft: 'auto' }}>{cmd.group}</span>
              </div>
              <div className="muted" style={{ fontSize: 12.5, whiteSpace: 'pre-line' }}>{cmd.help_text || 'No description.'}</div>
            </div>
            {cmd.params.length > 0 && (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 12 }}>
                {cmd.params.map(p => (
                  <div key={p.name} style={{ display: 'flex', flexDirection: 'column', gap: 5, minWidth: 0 }}>
                    <label htmlFor={`p-${p.name}`} className="mono muted" style={{ fontSize: 11.5 }} title={p.help_text}>
                      {p.name}{p.required ? ' *' : ''} <span style={{ fontFamily: 'var(--font-body)', opacity: .75 }}>· {p.type}</span>
                    </label>
                    {isToggle(p.type)
                      ? <Toggle on={!!value(p)} onFlip={() => setVals(v => ({ ...v, [p.name]: !value(p) }))} />
                      : <input id={`p-${p.name}`} className="input mono" type={p.type === 'int' || p.type === 'float' ? 'number' : 'text'}
                          value={value(p) ?? ''} placeholder={p.help_text?.slice(0, 40)} onChange={e => setVals(v => ({ ...v, [p.name]: e.target.value }))} style={{ fontSize: 12.5 }} />}
                  </div>
                ))}
              </div>
            )}
            <code className="term" style={{ fontSize: 11.5, padding: '7px 10px' }}><span style={{ color: 'var(--color-accent)' }}>$</span> {cmdLine}</code>
            {cmd.is_destructive && (
              <label style={{ display: 'flex', alignItems: 'flex-start', gap: 10, padding: '10px 12px', borderRadius: 'var(--radius-md)', border: '1px solid color-mix(in srgb, var(--st-crit) 40%, transparent)', background: 'color-mix(in srgb, var(--st-crit) 7%, transparent)', cursor: 'pointer', fontSize: 12.5 }}>
                <input type="checkbox" id="cmd-confirm" checked={confirm} onChange={e => setConfirm(e.target.checked)} style={{ width: 16, height: 16, margin: '1px 0 0', accentColor: 'var(--st-crit)', flex: 'none' }} />
                <span><span style={{ fontWeight: 500 }}>I've checked the parameters.</span> <span className="muted">This can change <span className="mono">{context || 'your systems'}</span> directly — it doesn't go through Slack approval.</span></span>
              </label>
            )}
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <button className={`btn ${cmd.is_destructive ? 'btn-danger' : 'btn-primary'}`} onClick={run} disabled={demo || running || (cmd.is_destructive && !confirm)}>
                <Icon name="ph-play" />Run {cmd.full_command}
              </button>
              <span className="muted" style={{ fontSize: 12 }}>{demo ? 'Running commands is disabled in demo mode.' : running ? 'Running…' : cmd.is_destructive && !confirm ? 'Tick the box to enable Run.' : ''}</span>
            </div>
          </div>
        )}
        <div className="surface" style={{ display: 'flex', flexDirection: 'column', background: 'var(--term)', overflow: 'hidden' }}>
          <div className="muted" style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', fontSize: 11.5, background: 'var(--rule) no-repeat bottom / 100% 1px' }}>
            <Icon name="ph-terminal-window" /><span style={{ flex: 1 }}>Output</span><span className="mono" style={{ color: ST[1] }}>{ST[0]}</span>
          </div>
          <div ref={outRef} className="mono" style={{ padding: '10px 12px 14px', minHeight: 180, maxHeight: 420, overflow: 'auto', fontSize: 12, lineHeight: 1.6 }}>
            {!lines && <div className="muted">Run a command to see its output here.</div>}
            {lines?.map((ln, i) => <div key={i} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: lineColor(ln) }}>{ln}</div>)}
            {running && <span className="cursor" />}
          </div>
        </div>
      </div>
    </div>
  )
}
