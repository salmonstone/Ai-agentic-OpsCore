import { useState, useEffect } from 'react'

/**
 * Full-screen "About AtlasOS" overlay.
 * Fetches GET /api/about (live project scan) and renders the same sections
 * as the CLI `agent about`, formatted for the browser.
 *
 * Controlled by the parent: renders nothing unless `open` is true.
 */
export default function AboutPanel({ open, onClose }) {
  const [data, setData]   = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!open) return
    setData(null); setError(null)
    fetch('/api/about')
      .then(r => r.json())
      .then(d => { d.error ? setError(d.error) : setData(d) })
      .catch(e => setError(String(e)))
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const s  = data?.structure
  const rt = data?.runtime
  const totalCases = s ? s.eval_suites.reduce((a, e) => a + (e.cases || 0), 0) : 0

  return (
    <div style={{
      position:'fixed', inset:0, zIndex:100,
      background:'rgba(5,5,5,0.92)', backdropFilter:'blur(3px)',
      display:'flex', flexDirection:'column',
      fontFamily:"'Courier New',monospace",
    }}>
      {/* header */}
      <div style={{
        display:'flex', alignItems:'center', justifyContent:'space-between',
        padding:'0 28px', height:66, borderBottom:'1px solid #1e1e1e', flexShrink:0,
        background:'linear-gradient(180deg,#0e0e0e 0%,#0a0a0a 100%)',
      }}>
        <div style={{ display:'flex', alignItems:'center', gap:14 }}>
          <div style={{ width:28, height:28, background:'#e05020', clipPath:'polygon(0 0,100% 0,100% 65%,65% 100%,0 100%)', boxShadow:'0 0 16px #e0502055' }} />
          <span style={{ color:'#fff', fontSize:19, fontWeight:700, letterSpacing:4 }}>ABOUT ATLASOS</span>
          <span style={{ fontSize:14, color:'#9a9a9a' }}>
            {s ? `v${data.version} · living documentation` : 'scanning…'}
          </span>
        </div>
        <button onClick={onClose} style={{
          background:'transparent', border:'1px solid #222', color:'#a6a6a6',
          cursor:'pointer', fontSize:16, lineHeight:1, padding:'7px 14px', transition:'all .15s',
        }}
          onMouseEnter={e => { e.currentTarget.style.borderColor='#e05020'; e.currentTarget.style.color='#e05020' }}
          onMouseLeave={e => { e.currentTarget.style.borderColor='#222'; e.currentTarget.style.color='#a6a6a6' }}>
          ✕ ESC
        </button>
      </div>

      {/* body */}
      <div style={{ flex:1, overflow:'auto', padding:'28px 32px', display:'flex', flexDirection:'column', gap:28 }}>
        {error && (
          <div style={{ background:'#1a0e0e', border:'1px solid #ef444440', padding:20, color:'#ef4444', fontSize:14 }}>
            Failed to load /api/about: {error}
          </div>
        )}
        {!data && !error && (
          <div style={{ color:'#9a9a9a', fontSize:15 }}>Scanning project…</div>
        )}

        {data && (
          <>
            <div style={{ fontSize:15, color:'#b0b0b0', lineHeight:1.6, maxWidth:860 }}>
              AtlasOS is an AI-native infrastructure operating system —
              <span style={{ color:'#d8d8d8' }}> observe · diagnose · heal · optimize</span>.
              Every number below comes from a live scan of the running project, not hardcoded values.
            </div>

            {/* SCALE */}
            <Section title="PROJECT SCALE">
              <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fill,minmax(160px,1fr))', gap:12 }}>
                <Stat label="PYTHON FILES"   value={s.total_files} />
                <Stat label="LINES OF CODE"  value={s.total_lines.toLocaleString()} />
                <Stat label="SKILLS"         value={data.skills.length} accent="#818cf8" />
                <Stat label="INTEGRATIONS"   value={s.integrations.length} accent="#818cf8" />
                <Stat label="EVAL SUITES"    value={s.eval_suites.length} accent="#22c55e" />
                <Stat label="EVAL CASES"     value={totalCases} accent="#22c55e" />
                <Stat label="CONFIG VARS"    value={s.config_vars.length} />
                <Stat label="DEPENDENCIES"   value={s.dependencies.length} />
              </div>
            </Section>

            {/* RUNTIME */}
            <Section title="CURRENT STATE">
              <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fill,minmax(240px,1fr))', gap:10 }}>
                <Row k="Cluster"    v={rt.kubectl_connected ? rt.cluster_name : 'not connected'} ok={rt.kubectl_connected} />
                <Row k="AWS"        v={rt.aws_configured ? rt.aws_profile : 'not set'} ok={rt.aws_configured} />
                <Row k="Daemon"     v={rt.daemon_running ? 'running' : 'stopped'} ok={rt.daemon_running} />
                <Row k="Webhook"    v={rt.webhook_running ? `listening :${rt.webhook_port}` : 'not running'} ok={rt.webhook_running} />
                <Row k="Anthropic"  v={rt.anthropic_configured ? 'key set' : 'missing'} ok={rt.anthropic_configured} />
                <Row k="Slack"      v={rt.slack_configured ? 'configured' : 'not set'} ok={rt.slack_configured} />
                <Row k="Gmail"      v={rt.gmail_configured ? 'connected' : 'not set'} ok={rt.gmail_configured} />
                <Row k="Memory"     v={`${rt.memory_records.toLocaleString()} records`} ok={rt.memory_records > 0} />
                <Row k="Embeddings" v={`${rt.embeddings_count.toLocaleString()} vectors`} ok={rt.embeddings_count > 0} />
                <Row k="Deploys"    v={`${rt.total_deploys} rec · ${rt.pending_deploys} pending`} ok={true} />
                <Row k="Last activity" v={rt.last_activity} ok={true} />
              </div>
            </Section>

            {/* SKILLS */}
            <Section title={`INSTALLED SKILLS · ${data.skills.length}`}>
              <div style={{ border:'1px solid #1e1e1e' }}>
                {data.skills.map((sk, i) => (
                  <div key={sk.name} style={{
                    display:'grid', gridTemplateColumns:'190px 1fr 120px', gap:14,
                    padding:'12px 18px', fontSize:14,
                    borderBottom: i < data.skills.length - 1 ? '1px solid #161616' : 'none',
                    background: i % 2 ? '#0c0c0c' : '#0a0a0a',
                  }}>
                    <span style={{ color:'#e05020' }}>{sk.label}</span>
                    <span style={{ color:'#b4b4b4' }}>{sk.description}</span>
                    <span style={{ color: sk.eval_suite ? '#22c55e' : '#858585', textAlign:'right' }}>
                      {sk.eval_suite ? `✓ ${sk.eval_cases || ''} cases`.trim() : '—'}
                    </span>
                  </div>
                ))}
              </div>
            </Section>

            {/* TECH STACK */}
            <Section title="TECHNOLOGY CHOICES">
              <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fill,minmax(300px,1fr))', gap:10 }}>
                {TECH.map(([c, why]) => (
                  <div key={c} style={{ background:'#0d0d0d', border:'1px solid #1a1a1a', padding:'12px 16px' }}>
                    <div style={{ color:'#d8d8d8', fontSize:14 }}>{c}</div>
                    <div style={{ color:'#9a9a9a', fontSize:13, marginTop:4 }}>{why}</div>
                  </div>
                ))}
              </div>
            </Section>

            {/* PRODUCTION-GRADE */}
            <Section title="WHAT MAKES THIS PRODUCTION-GRADE">
              <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fill,minmax(320px,1fr))', gap:9 }}>
                <Feature text={`Eval suites: ${totalCases} cases across ${s.eval_suites.length} suites`} />
                {FEATURES.map(f => <Feature key={f} text={f} />)}
              </div>
            </Section>

            <div style={{ fontSize:13, color:'#858585', paddingTop:8 }}>
              Regenerated from the live project on every load · CLI equivalent:
              <span style={{ color:'#a6a6a6' }}> agent about</span>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function Section({ title, children }) {
  return (
    <div style={{ display:'flex', flexDirection:'column', gap:14 }}>
      <div style={{ fontSize:13, letterSpacing:2, color:'#a6a6a6', borderBottom:'1px solid #1a1a1a', paddingBottom:8 }}>
        {title}
      </div>
      {children}
    </div>
  )
}

function Stat({ label, value, accent = '#fff' }) {
  return (
    <div style={{ background:'#0d0d0d', border:'1px solid #1a1a1a', borderLeft:`3px solid ${accent}`, padding:'16px 18px' }}>
      <div style={{ fontSize:12, letterSpacing:1.5, color:'#9a9a9a', marginBottom:10 }}>{label}</div>
      <div style={{ fontSize:30, fontWeight:700, color:accent, lineHeight:1 }}>{value}</div>
    </div>
  )
}

function Row({ k, v, ok }) {
  return (
    <div style={{ display:'flex', justifyContent:'space-between', gap:10, padding:'9px 14px', background:'#0c0c0c', border:'1px solid #161616' }}>
      <span style={{ fontSize:14, color:'#9a9a9a' }}>{k}</span>
      <span style={{ fontSize:14, color: ok ? '#22c55e' : '#9a9a9a', textAlign:'right' }}>{v}</span>
    </div>
  )
}

function Feature({ text }) {
  return (
    <div style={{ fontSize:14, color:'#c0c0c0', display:'flex', gap:8 }}>
      <span style={{ color:'#22c55e' }}>✓</span>{text}
    </div>
  )
}

const TECH = [
  ['Python 3.11',      'AI ecosystem alignment'],
  ['Claude',           'strong reasoning for agentic ops'],
  ['Raw Anthropic SDK','full control, debuggable — no framework'],
  ['SQLite',           'embedded source of truth, zero-ops'],
  ['ChromaDB',         'local vector store, no infra'],
  ['Typer',            'type-hint driven CLI'],
  ['Rich',             'terminal tables, panels, colors'],
  ['Pydantic v2',      'type safety across boundaries'],
  ['FastAPI',          'async dashboard API, same codebase'],
  ['React (esbuild)',  'dashboard UI'],
  ['WebSocket',        'real-time live updates'],
  ['structlog',        'JSON, queryable logs'],
]

const FEATURES = [
  'Alert deduplication (cooldown + escalation)',
  'Auto-rollback on deploy failure',
  'False-positive filtering (security scan)',
  'Human-in-the-loop approval gates',
  'Production namespace confirmation',
  'Cost tracking on every Claude call',
  'Full audit log (SQLite)',
  'Persistent memory with RAG',
  'Webhook HMAC verification',
  'Secrets in env vars only',
]
