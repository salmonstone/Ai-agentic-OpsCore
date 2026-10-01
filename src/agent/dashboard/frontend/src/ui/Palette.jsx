import { useEffect, useMemo, useRef, useState } from 'react'
import { Icon } from './common'

/** Ctrl/⌘+K: jump to a panel, run a quick action, open any CLI command, ask
 *  the assistant, or flip a setting — from anywhere, keyboard only. */
export default function Palette({ open, onClose, nav, quick, commands, onNav, onRun, onPickCommand, onAsk, toggles }) {
  const [q, setQ] = useState('')
  const [idx, setIdx] = useState(0)
  const inputRef = useRef(null)
  const listRef = useRef(null)

  useEffect(() => { if (open) { setQ(''); setIdx(0); setTimeout(() => inputRef.current?.focus(), 0) } }, [open])

  const items = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const match = s => !needle || s.toLowerCase().includes(needle)
    const out = []
    for (const n of nav) if (match(`go ${n.label}`)) out.push({ group: 'Go to', icon: n.icon, label: n.label, run: () => onNav(n.id) })
    for (const a of quick) if (match(`run ${a.name} ${a.cmd || ''}`)) out.push({ group: 'Quick actions', icon: a.icon, label: a.name, hint: a.cmd ? `agent ${a.cmd}` : '', run: () => onRun(a) })
    for (const t of toggles) if (match(t.label)) out.push({ group: 'Settings', icon: t.icon, label: t.label, run: t.run })
    if (needle) {
      const cmds = commands.filter(c => match(c.full_command)).slice(0, 8)
      for (const c of cmds) out.push({ group: 'Commands', icon: 'ph-terminal', label: c.full_command, hint: c.is_destructive ? 'changes things' : '', mono: true, run: () => onPickCommand(c.full_command) })
      out.push({ group: 'Assistant', icon: 'ph-sparkle', label: `Ask: “${q.trim()}”`, run: () => onAsk(q.trim()) })
    }
    return out
  }, [q, nav, quick, commands, toggles, onNav, onRun, onPickCommand, onAsk])

  useEffect(() => { setIdx(0) }, [q])
  useEffect(() => { listRef.current?.querySelector(`[data-i="${idx}"]`)?.scrollIntoView({ block: 'nearest' }) }, [idx])

  if (!open) return null
  const choose = it => { onClose(); it.run() }
  const onKey = e => {
    if (e.key === 'Escape') onClose()
    else if (e.key === 'ArrowDown') { e.preventDefault(); setIdx(i => Math.min(items.length - 1, i + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setIdx(i => Math.max(0, i - 1)) }
    else if (e.key === 'Enter' && items[idx]) { e.preventDefault(); choose(items[idx]) }
  }

  let lastGroup = null
  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, zIndex: 60, display: 'flex', justifyContent: 'center', alignItems: 'flex-start', paddingTop: '12vh', background: 'color-mix(in srgb, var(--color-bg) 65%, transparent)' }}>
      <div role="dialog" aria-label="Command palette" onClick={e => e.stopPropagation()} style={{ width: 'min(600px, calc(100vw - 32px))', borderRadius: 'var(--radius-lg)', background: 'var(--color-surface)', boxShadow: 'var(--shadow-lg)', overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 14px', background: 'var(--rule) no-repeat bottom / 100% 1px' }}>
          <Icon name="ph-magnifying-glass" size={17} style={{ color: 'var(--muted)' }} />
          <input ref={inputRef} id="palette-input" value={q} onChange={e => setQ(e.target.value)} onKeyDown={onKey}
            placeholder="Go to, run, or ask AtlasOS…" aria-label="Search"
            style={{ flex: 1, border: 0, outline: 'none', background: 'transparent', color: 'var(--color-text)', font: 'inherit', fontSize: 15 }} />
          <kbd className="mono muted" style={{ fontSize: 10.5, padding: '2px 6px', borderRadius: 4, border: '1px solid var(--color-divider)' }}>esc</kbd>
        </div>
        <div ref={listRef} style={{ maxHeight: '50vh', overflow: 'auto', padding: 6 }}>
          {items.length === 0 && <div className="muted" style={{ padding: 14, fontSize: 12.5 }}>Nothing matches.</div>}
          {items.map((it, i) => {
            const head = it.group !== lastGroup ? (lastGroup = it.group) : null
            return (
              <div key={`${it.group}-${it.label}`}>
                {head && <div className="kicker" style={{ padding: '8px 10px 4px', fontSize: 10.5 }}>{head}</div>}
                <button data-i={i} onClick={() => choose(it)} onMouseMove={() => setIdx(i)} style={{
                  display: 'flex', alignItems: 'center', gap: 10, width: '100%', padding: '8px 10px', border: 0, borderRadius: 6, cursor: 'pointer', textAlign: 'left',
                  background: i === idx ? 'color-mix(in srgb, var(--color-accent) 16%, transparent)' : 'transparent',
                }}>
                  <Icon name={it.icon} size={16} style={{ color: 'var(--color-accent)' }} />
                  <span style={{ flex: 1, fontSize: 13, fontFamily: it.mono ? 'var(--font-mono)' : undefined }}>{it.label}</span>
                  {it.hint && <span className="mono muted" style={{ fontSize: 11 }}>{it.hint}</span>}
                </button>
              </div>
            )
          })}
        </div>
        <div className="muted" style={{ display: 'flex', gap: 14, padding: '8px 14px', fontSize: 11, background: 'var(--rule) no-repeat top / 100% 1px' }}>
          <span>↑↓ move</span><span>↵ open</span><span>type a question to ask the assistant</span>
        </div>
      </div>
    </div>
  )
}
