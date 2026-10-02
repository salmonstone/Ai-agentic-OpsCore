import { useEffect, useRef } from 'react'
import { lineColor, tone } from '../lib/tone'
import { Icon } from './common'

export default function RunDrawer({ run, onClose, onRerun, onAsk }) {
  const termRef = useRef(null)
  useEffect(() => { if (termRef.current) termRef.current.scrollTop = termRef.current.scrollHeight }, [run?.lines])
  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  if (!run) return null

  const running = run.status === 'running'
  const st = running ? { ...tone('accent'), label: 'running', icon: 'ph-circle-notch' }
    : run.status === 'ok' ? { ...tone('ok'), label: 'done' } : { ...tone('crit'), label: 'failed' }

  return (
    <>
      <div onClick={onClose} style={{ position: 'fixed', inset: 0, zIndex: 50, background: 'color-mix(in srgb, var(--color-bg) 60%, transparent)' }} />
      <aside role="dialog" aria-label="Run output" style={{ position: 'fixed', top: 0, right: 0, bottom: 0, zIndex: 51, width: 'min(560px, 100vw)', display: 'flex', flexDirection: 'column', background: 'var(--color-surface)', boxShadow: 'var(--shadow-lg)' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, padding: '16px 16px 12px', background: 'var(--rule) no-repeat bottom / 100% 1px' }}>
          <Icon name={run.icon || 'ph-terminal-window'} size={20} style={{ color: 'var(--color-accent)', marginTop: 2 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 16, fontWeight: 500 }}>{run.name}</div>
            <code className="muted" style={{ fontSize: 11.5 }}>$ {run.cmd}</code>
          </div>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, padding: '2px 9px', borderRadius: 6, fontSize: 11.5, fontWeight: 500, whiteSpace: 'nowrap', color: st.c, background: st.tint, border: `1px solid ${st.line}` }}>
            <Icon name={st.icon} className={running ? 'spin' : undefined} />{st.label}
          </span>
          <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close"><Icon name="ph-x" size={16} /></button>
        </div>
        <div ref={termRef} className="mono" style={{ flex: 1, overflow: 'auto', margin: '12px 16px', padding: '12px 14px', borderRadius: 'var(--radius-md)', background: 'var(--term)', fontSize: 12, lineHeight: 1.65 }}>
          {run.lines.map((ln, i) => <div key={i} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: lineColor(ln) }}>{ln}</div>)}
          {running && <span className="cursor" />}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '0 16px 16px' }}>
          <button className="btn btn-secondary" onClick={() => onAsk(run)} disabled={running}><Icon name="ph-chat-circle-dots" />Ask assistant about this</button>
          {onRerun && <button className="btn btn-ghost" onClick={onRerun} disabled={running}><Icon name="ph-arrow-clockwise" />Run again</button>}
          <button className="btn btn-secondary" onClick={onClose} style={{ marginLeft: 'auto' }}>Close</button>
        </div>
      </aside>
    </>
  )
}
