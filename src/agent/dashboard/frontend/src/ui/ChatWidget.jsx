import { useCallback, useEffect, useRef, useState } from 'react'
import { ago, streamSSE } from '../lib/api'
import { useDemo } from '../lib/demo'
import { Icon } from './common'
import Markdown from './Markdown'

// Follow-up suggestions chosen from which tools the answer used — no extra
// model call. First matching prefix wins.
const FOLLOW_UPS = [
  ['k8s_', ['Diagnose the worst pod', 'Show node CPU and memory', 'What did the daemon heal today?']],
  ['jenkins_', ['Can this be fixed automatically?', 'Show recent builds of that job', 'Any other failing jobs?']],
  ['github_actions', ['Diagnose the latest failed run', 'Is main passing?']],
  ['cost_', ["What's driving the cost?", 'Any idle resources I can remove?']],
  ['aws_', ["What's driving the cost?", 'Any unhealthy AWS resources?']],
  ['tls_', ['Which certificates expire soonest?', 'Can AtlasOS renew them?']],
  ['daily_summary', ["What's the most urgent thing?", 'Show pending approvals']],
]
const DEFAULT_FOLLOW_UPS = ["What's broken right now?", 'Show pending approvals']
function followUpsFor(tools) {
  for (const [prefix, list] of FOLLOW_UPS) if (tools.some(t => t.startsWith(prefix))) return list
  return DEFAULT_FOLLOW_UPS
}

function CopyButton({ text }) {
  const [done, setDone] = useState(false)
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500) } catch { /* clipboard blocked */ }
  }
  return (
    <button className="btn btn-ghost" onClick={copy} title="Copy answer" aria-label="Copy answer" style={{ padding: '1px 4px', fontSize: 11, color: 'var(--muted)' }}>
      <Icon name={done ? 'ph-check' : 'ph-copy'} />{done ? 'Copied' : 'Copy'}
    </button>
  )
}

const SUGGESTIONS = [
  { icon: 'ph-warning-circle', text: "What's broken right now?" },
  { icon: 'ph-cube-focus', text: 'Scan my cluster' },
  { icon: 'ph-hammer', text: 'Why did the last Jenkins build fail?' },
  { icon: 'ph-currency-dollar', text: 'Show AWS spend this week' },
]

function sessionId() {
  try {
    let id = sessionStorage.getItem('atlas-chat-session')
    if (!id) { id = crypto.randomUUID(); sessionStorage.setItem('atlas-chat-session', id) }
    return id
  } catch {
    return 'session-' + Math.random().toString(36).slice(2)
  }
}

const fmtParams = input => Object.entries(input || {}).map(([k, v]) => `${k.padEnd(12)} ${typeof v === 'object' ? JSON.stringify(v) : v}`).join('\n') || '(no parameters)'

/** One streamed turn (a send or a decide) -> a reducer over the message list. */
function useThread() {
  const [messages, setMessages] = useState([])
  const [followUps, setFollowUps] = useState([])
  const textId = useRef(null)
  const lastText = useRef(null)
  const turnTools = useRef([])
  const seq = useRef(0)
  const nid = () => `m${++seq.current}`
  const patch = (id, p) => setMessages(ms => ms.map(m => (m.id === id ? { ...m, ...p } : m)))

  const onEvent = useCallback(ev => {
    if (ev.type === 'done') {
      if (ev.usage && lastText.current) patch(lastText.current, { usage: ev.usage })
      if (ev.stop === 'end_turn') setFollowUps(followUpsFor(turnTools.current))
    }
    if (ev.type === 'tool_start') turnTools.current.push(ev.name)
    if (ev.type === 'text') {
      if (!textId.current) {
        const id = nid(); textId.current = id; lastText.current = id
        setMessages(ms => [...ms, { id, kind: 'text', text: ev.delta, streaming: true }])
      } else {
        const id = textId.current
        setMessages(ms => ms.map(m => (m.id === id ? { ...m, text: m.text + ev.delta } : m)))
      }
      return
    }
    if (textId.current) { patch(textId.current, { streaming: false }); textId.current = null }
    if (ev.type === 'tool_start') {
      setMessages(ms => ms.some(m => m.kind === 'confirm' && m.toolId === ev.id)
        ? ms : [...ms, { id: nid(), kind: 'chip', toolId: ev.id, name: ev.name, running: true }])
    } else if (ev.type === 'tool_end') {
      setMessages(ms => ms.map(m => (m.kind === 'chip' && m.toolId === ev.id ? { ...m, running: false, ok: ev.ok, ms: ev.ms, result: ev.result } : m)))
    } else if (ev.type === 'confirm') {
      setMessages(ms => [...ms, { id: nid(), kind: 'confirm', toolId: ev.id, name: ev.name, title: ev.title, params: fmtParams(ev.input), status: 'pending' }])
    } else if (ev.type === 'confirm_result') {
      setMessages(ms => ms.map(m => (m.kind === 'confirm' && m.toolId === ev.id ? { ...m, status: ev.status, message: ev.message } : m)))
    } else if (ev.type === 'error') {
      setMessages(ms => [...ms, { id: nid(), kind: 'error', text: ev.message, detail: ev.detail }])
    }
  }, [])

  const push = m => setMessages(ms => [...ms, { id: nid(), ...m }])
  const startTurn = () => { turnTools.current = []; lastText.current = null; setFollowUps([]) }
  const endTurn = () => { if (textId.current) { patch(textId.current, { streaming: false }); textId.current = null } }
  return { messages, setMessages, onEvent, push, startTurn, endTurn, patch, followUps, setFollowUps }
}

function Chip({ m }) {
  const [open, setOpen] = useState(false)
  const icon = m.running ? 'ph-circle-notch' : m.ok ? 'ph-check-circle' : 'ph-x-circle'
  const c = m.running ? 'var(--color-accent)' : m.ok ? 'var(--st-ok)' : 'var(--st-crit)'
  const label = m.running ? `Running ${m.name}…` : `${m.name} · ${m.ok ? `${(m.ms / 1000).toFixed(1)}s` : 'failed'}`
  return (
    <div style={{ alignSelf: 'flex-start', maxWidth: '100%', display: 'flex', flexDirection: 'column', gap: 6 }}>
      <button onClick={() => !m.running && setOpen(o => !o)} aria-expanded={open} className="hoverable" style={{
        display: 'inline-flex', alignItems: 'center', gap: 7, alignSelf: 'flex-start', padding: '4px 10px 4px 8px', borderRadius: 999,
        border: '1px solid var(--color-divider)', background: 'color-mix(in srgb, var(--color-text) 4%, transparent)',
        fontFamily: 'var(--font-mono)', fontSize: 11.5, cursor: m.running ? 'default' : 'pointer',
      }}>
        <Icon name={icon} className={m.running ? 'spin' : undefined} style={{ color: c }} /><span>{label}</span>
        {!m.running && <Icon name={open ? 'ph-caret-up' : 'ph-caret-down'} style={{ color: 'var(--muted)' }} />}
      </button>
      {open && <pre className="term" style={{ margin: 0, maxHeight: 180, overflow: 'auto', padding: '9px 11px', fontSize: 11, lineHeight: 1.5 }}>{m.result || '(no output)'}</pre>}
    </div>
  )
}

function ConfirmCard({ m, onDecide }) {
  const pending = m.status === 'pending'
  const crit = pending || m.status === 'deciding'
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', gap: 10, padding: 12, borderRadius: 'var(--radius-md)',
      border: `1px solid ${crit ? 'color-mix(in srgb, var(--st-crit) 45%, transparent)' : 'var(--color-divider)'}`,
      background: crit ? 'color-mix(in srgb, var(--st-crit) 6%, transparent)' : 'transparent',
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 9 }}>
        <Icon name="ph-shield-warning" size={18} style={{ color: 'var(--st-crit)', marginTop: 1 }} />
        <div style={{ minWidth: 0 }}>
          <div className="kicker" style={{ fontSize: 10.5 }}>AtlasOS wants to run</div>
          <div className="mono" style={{ fontSize: 13.5, fontWeight: 500, lineHeight: 1.3, wordBreak: 'break-word' }}>{m.name}</div>
        </div>
      </div>
      <pre className="term" style={{ margin: 0, padding: '9px 11px', fontSize: 11.5, lineHeight: 1.55 }}>{m.params}</pre>
      {pending && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div className="muted" style={{ fontSize: 11.5 }}>Nothing runs until you confirm.</div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn btn-danger" style={{ flex: 1 }} onClick={() => onDecide(m, true)}><Icon name="ph-check" />Confirm</button>
            <button className="btn btn-secondary" style={{ flex: 1 }} onClick={() => onDecide(m, false)}>Cancel</button>
          </div>
        </div>
      )}
      {(m.status === 'deciding' || m.status === 'applying') && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, color: 'var(--color-accent)' }}><Icon name="ph-circle-notch" className="spin" />Applying…</div>
      )}
      {m.status === 'applied' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12.5 }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 500, color: 'var(--st-ok)' }}><Icon name="ph-check-circle" />Applied</span>
          {m.message && <span className="mono muted" style={{ fontSize: 11.5 }}>{m.message}</span>}
        </div>
      )}
      {m.status === 'failed' && (
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 6, fontSize: 12.5, color: 'var(--st-crit)' }}><Icon name="ph-x-circle" style={{ marginTop: 2 }} /><span>Failed: {m.message || 'the tool reported an error'}</span></div>
      )}
      {m.status === 'cancelled' && (
        <div className="muted" style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5 }}><Icon name="ph-prohibit" />Cancelled — nothing changed.</div>
      )}
    </div>
  )
}

const SEV_RANK = { critical: 0, high: 0, sev1: 0, warning: 1, medium: 1, sev2: 1, low: 2, info: 2 }

/** Incident-aware quick view: the top open incident, from real data only —
 *  "likely cause" is the incident's own recorded cause, never an invented
 *  diagnosis. A real AI diagnosis is one click away, through the same chat. */
function DiagnoseTab({ incidents, onDiagnose, onViewAll }) {
  const open = (incidents?.data || []).filter(i => i.status === 'open')
    .sort((a, b) => (SEV_RANK[(a.severity || '').toLowerCase()] ?? 2) - (SEV_RANK[(b.severity || '').toLowerCase()] ?? 2) || Date.parse(b.opened_at) - Date.parse(a.opened_at))
  if (!incidents?.data) return <div className="muted" style={{ padding: 16, fontSize: 12.5, display: 'flex', alignItems: 'center', gap: 6 }}><Icon name="ph-circle-notch" className="spin" />Loading incidents…</div>
  if (open.length === 0) return (
    <div style={{ padding: 16, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, textAlign: 'center', color: 'var(--st-ok)' }}>
      <Icon name="ph-check-circle" size={26} /><div style={{ fontSize: 13, color: 'var(--color-text)' }}>No open incidents</div>
      <div className="muted" style={{ fontSize: 12 }}>Ask the assistant anything in Chat, or check in again if something comes up.</div>
    </div>
  )
  const i = open[0]
  const crit = (i.severity || '').toLowerCase() in SEV_RANK && SEV_RANK[(i.severity || '').toLowerCase()] === 0
  return (
    <div style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: 13, borderRadius: 'var(--radius-md)',
        border: `1px solid color-mix(in srgb, ${crit ? 'var(--st-crit)' : 'var(--st-warn)'} 45%, transparent)`,
        background: `color-mix(in srgb, ${crit ? 'var(--st-crit)' : 'var(--st-warn)'} 7%, transparent)` }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Icon name="ph-siren" style={{ color: crit ? 'var(--st-crit)' : 'var(--st-warn)' }} />
          <span className="kicker" style={{ fontSize: 10.5, color: crit ? 'var(--st-crit)' : 'var(--st-warn)' }}>Incident detected{open.length > 1 ? ` · ${open.length} open` : ''}</span>
        </div>
        <div style={{ fontSize: 14, fontWeight: 500, lineHeight: 1.3 }}>{i.title}</div>
        <div className="mono muted" style={{ fontSize: 11 }}>{[i.service, i.namespace].filter(Boolean).join('/')} · opened {ago(i.opened_at)}</div>
        {i.cause && (
          <div>
            <div className="kicker" style={{ fontSize: 10 }}>Likely cause</div>
            <div style={{ fontSize: 12.5 }}>{i.cause}</div>
          </div>
        )}
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button className="btn btn-primary" onClick={() => onDiagnose(i)} style={{ fontSize: 12.5 }}><Icon name="ph-sparkle" />Diagnose with AI</button>
          <button className="btn btn-secondary" onClick={onViewAll} style={{ fontSize: 12.5 }}><Icon name="ph-list-bullets" />View evidence</button>
        </div>
      </div>
      {open.length > 1 && <div className="muted" style={{ fontSize: 11.5 }}>+{open.length - 1} more open — see Incidents &amp; SLOs.</div>}
    </div>
  )
}

export default function ChatWidget({ open, setOpen, askRequest, context, info, incidents, onNav, variant = 'overlay' }) {
  const { messages, setMessages, onEvent, push, startTurn, endTurn, patch, followUps, setFollowUps } = useThread()
  const demo = useDemo()
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [tab, setTab] = useState('chat')
  const sid = useRef(sessionId())
  const threadRef = useRef(null)
  const lastAsk = useRef(null)
  const panel = variant === 'panel'

  useEffect(() => { if (threadRef.current) threadRef.current.scrollTop = threadRef.current.scrollHeight }, [messages, open])

  const stream = useCallback(async (url, body) => {
    setBusy(true)
    startTurn()
    try {
      await streamSSE(url, body, onEvent)
    } catch (e) {
      onEvent({ type: 'error', message: "Couldn't reach the dashboard server.", detail: e.message })
    }
    endTurn()
    setBusy(false)
  }, [onEvent, startTurn, endTurn])

  const send = useCallback(text => {
    const t = text.trim()
    if (!t || busy) return
    setOpen(true)
    push({ kind: 'user', text: t })
    setInput('')
    stream('/api/chat', { session_id: sid.current, message: t })
  }, [busy, push, setOpen, stream])

  useEffect(() => {
    if (askRequest && askRequest !== lastAsk.current) { lastAsk.current = askRequest; send(askRequest.text) }
  }, [askRequest, send])

  const decide = (m, approve) => {
    if (busy) return
    patch(m.id, { status: 'deciding' })
    stream('/api/chat/decide', { session_id: sid.current, tool_use_id: m.toolId, approve })
  }

  const clear = async () => {
    setMessages([])
    setFollowUps([])
    try { await fetch(`/api/chat/${sid.current}`, { method: 'DELETE' }) } catch { /* ignore */ }
  }

  const retry = m => {
    const lastUser = [...messages].reverse().find(x => x.kind === 'user')
    if (lastUser) send(lastUser.text)
  }
  const diagnose = i => {
    setTab('chat')
    send(`Summarize incident ${i.id} ("${i.title}") and what's likely causing it, then recommend a fix.`)
  }

  const hasPendingConfirm = messages.some(m => m.kind === 'confirm' && m.status === 'pending')

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} aria-label="Open AtlasOS Assistant" style={{
        position: 'fixed', right: 20, bottom: 20, zIndex: 40, width: 54, height: 54, borderRadius: '50%',
        border: '1px solid var(--color-accent)', background: 'var(--color-surface)', color: 'var(--color-accent)',
        boxShadow: 'var(--shadow-md), 0 0 18px color-mix(in srgb, var(--color-accent) 28%, transparent)', display: 'grid', placeItems: 'center', cursor: 'pointer',
      }}>
        <Icon name="ph-chat-circle-dots" size={24} />
        {hasPendingConfirm && <span style={{ position: 'absolute', top: 3, right: 3, width: 12, height: 12, borderRadius: '50%', background: 'var(--st-crit)', boxShadow: '0 0 0 2px var(--color-bg)' }} />}
      </button>
    )
  }

  const mode = info?.data ? (info.data.mutations_enabled ? 'asks before changes' : 'read-only') : 'loading tools…'
  const openCount = (incidents?.data || []).filter(i => i.status === 'open').length
  return (
    <section aria-label="AtlasOS Assistant" style={panel ? {
      position: 'sticky', top: 'var(--topbar-h)', height: 'calc(100vh - var(--topbar-h))', display: 'flex', flexDirection: 'column',
      background: 'var(--color-bg-2)', boxShadow: 'inset 1px 0 0 var(--color-divider)', overflow: 'hidden',
    } : {
      position: 'fixed', right: 20, bottom: 20, zIndex: 40, width: 400, maxWidth: 'calc(100vw - 40px)', height: 'min(620px, calc(100vh - 40px))',
      display: 'flex', flexDirection: 'column', borderRadius: 'var(--radius-lg)', background: 'var(--color-surface)', boxShadow: 'var(--shadow-lg)', overflow: 'hidden',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 10px 10px 14px', background: 'var(--rule) no-repeat bottom / 100% 1px' }}>
        <Icon name="ph-sparkle" size={18} style={{ color: 'var(--color-accent)' }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ fontSize: 14, fontWeight: 500 }}>AtlasOS Assistant</span>
            {!demo && <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 10.5, color: 'var(--st-ok)' }}><span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--st-ok)' }} />Online</span>}
          </div>
          <div className="muted" style={{ fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {demo
              ? <span style={{ color: 'var(--st-warn)' }}>Uses your live systems, not the demo data</span>
              : <>Runs skills on <span className="mono">{context || 'no cluster'}</span> · {mode}</>}
          </div>
        </div>
        <button className="btn btn-ghost btn-icon" onClick={clear} aria-label="Clear chat" title="Clear chat" disabled={busy}><Icon name="ph-trash" size={16} /></button>
        <button className="btn btn-ghost btn-icon" onClick={() => setOpen(false)} aria-label={panel ? 'Collapse' : 'Minimize'} title={panel ? 'Collapse' : 'Minimize'}><Icon name={panel ? 'ph-sidebar-simple' : 'ph-minus'} size={16} /></button>
      </div>

      {panel && (
        <div role="tablist" style={{ display: 'flex', gap: 4, padding: '8px 10px 0' }}>
          {[['chat', 'Chat', null], ['diagnose', 'Diagnose', openCount || null]].map(([id, label, badge]) => (
            <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)} style={{
              display: 'flex', alignItems: 'center', gap: 6, padding: '6px 12px', border: 0, borderRadius: 'var(--radius-md) var(--radius-md) 0 0', cursor: 'pointer', fontSize: 12.5,
              background: tab === id ? 'var(--color-surface)' : 'transparent', color: tab === id ? 'var(--color-text)' : 'var(--muted)',
            }}>
              {label}{badge ? <span className="mono" style={{ fontSize: 10, padding: '0 5px', borderRadius: 8, color: 'var(--st-crit)', background: 'color-mix(in srgb, var(--st-crit) 16%, transparent)' }}>{badge}</span> : null}
            </button>
          ))}
        </div>
      )}

      {panel && tab === 'diagnose' ? (
        <div style={{ flex: 1, overflow: 'auto', background: 'var(--color-surface)' }}>
          <DiagnoseTab incidents={incidents} onDiagnose={diagnose} onViewAll={() => onNav?.('incidents')} />
        </div>
      ) : (
      <>
      <div ref={threadRef} style={{ flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 10, padding: 14, background: panel ? 'var(--color-surface)' : undefined }}>
        {messages.length === 0 && (
          <div style={{ marginTop: 'auto', display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ fontSize: 15, fontWeight: 500 }}>Ask about your cluster, builds or spend.</div>
            <div className="muted" style={{ fontSize: 12.5 }}>I answer by running AtlasOS skills. Anything that changes your systems waits for your Confirm.</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 4 }}>
              {SUGGESTIONS.map(s => (
                <button key={s.text} onClick={() => send(s.text)} className="hoverable" style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '8px 10px', borderRadius: 'var(--radius-md)', border: '1px solid var(--color-divider)', background: 'transparent', textAlign: 'left', fontSize: 12.5 }}>
                  <Icon name={s.icon} size={15} style={{ color: 'var(--color-accent)' }} />{s.text}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map(m => {
          if (m.kind === 'user') return (
            <div key={m.id} style={{ alignSelf: 'flex-end', maxWidth: '85%', padding: '8px 11px', borderRadius: '12px 12px 4px 12px', background: 'color-mix(in srgb, var(--color-accent) 16%, var(--color-surface))', border: '1px solid color-mix(in srgb, var(--color-accent) 32%, transparent)', fontSize: 13, whiteSpace: 'pre-wrap' }}>{m.text}</div>
          )
          if (m.kind === 'text') return (
            <div key={m.id} style={{ alignSelf: 'flex-start', maxWidth: '94%', display: 'flex', flexDirection: 'column', gap: 3 }}>
              <div style={{ padding: '9px 12px', borderRadius: '12px 12px 12px 4px', background: 'color-mix(in srgb, var(--color-text) 5%, transparent)', fontSize: 13, lineHeight: 1.5, wordBreak: 'break-word' }}>
                <Markdown text={m.text} />{m.streaming && <span className="cursor" />}
              </div>
              {!m.streaming && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingLeft: 4 }}>
                  <CopyButton text={m.text} />
                  {m.usage?.cost_usd > 0 && (
                    <span className="mono muted" style={{ fontSize: 10.5 }} title={`${m.usage.input_tokens} input + ${m.usage.output_tokens} output tokens`}>
                      ${m.usage.cost_usd < 0.01 ? m.usage.cost_usd.toFixed(4) : m.usage.cost_usd.toFixed(3)} · {(m.usage.input_tokens + m.usage.output_tokens).toLocaleString()} tokens
                    </span>
                  )}
                </div>
              )}
            </div>
          )
          if (m.kind === 'chip') return <Chip key={m.id} m={m} />
          if (m.kind === 'confirm') return <ConfirmCard key={m.id} m={m} onDecide={decide} />
          if (m.kind === 'error') return (
            <div key={m.id} style={{ alignSelf: 'flex-start', maxWidth: '94%', display: 'flex', gap: 10, padding: '10px 12px', borderRadius: '12px 12px 12px 4px', border: '1px dashed var(--st-unk)', background: 'var(--hatch), color-mix(in srgb, var(--st-unk) 7%, var(--color-surface))' }}>
              <Icon name="ph-question" size={17} style={{ color: 'var(--st-unk)', marginTop: 1 }} />
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
                <div style={{ fontSize: 13 }}>{m.text}</div>
                {m.detail && <div className="mono muted" style={{ fontSize: 11, wordBreak: 'break-word' }}>{m.detail}</div>}
                <button className="btn btn-ghost" onClick={() => retry(m)} disabled={busy} style={{ alignSelf: 'flex-start', fontSize: 12, padding: '2px 6px', marginTop: 2 }}><Icon name="ph-arrow-clockwise" />Try again</button>
              </div>
            </div>
          )
          return null
        })}
        {!busy && followUps.length > 0 && messages.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 2 }}>
            {followUps.map(f => (
              <button key={f} onClick={() => send(f)} className="hoverable" style={{ padding: '4px 10px', borderRadius: 999, border: '1px solid var(--color-divider)', background: 'transparent', fontSize: 11.5, color: 'var(--color-accent)' }}>
                {f}
              </button>
            ))}
          </div>
        )}
        {busy && !messages.some(m => m.streaming || (m.kind === 'chip' && m.running)) && (
          <div className="muted" style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6 }}><Icon name="ph-circle-notch" className="spin" />Thinking…</div>
        )}
      </div>

      <form onSubmit={e => { e.preventDefault(); send(input) }} style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '10px 12px 12px', background: panel ? 'var(--color-surface)' : 'var(--rule) no-repeat top / 100% 1px', boxShadow: panel ? 'inset 0 1px 0 var(--color-divider)' : undefined }}>
        <input id="chat-input" className="input" value={input} onChange={e => setInput(e.target.value)} placeholder={hasPendingConfirm ? 'Confirm or cancel above, or ask something else…' : 'Ask anything about your infrastructure…'} aria-label="Message" style={{ flex: 1, minHeight: 38 }} />
        <button type="submit" className="btn btn-primary" aria-label="Send" disabled={busy || !input.trim()} style={{ width: 38, height: 38, padding: 0 }}><Icon name="ph-paper-plane-right" size={16} /></button>
      </form>
      </>
      )}
    </section>
  )
}
