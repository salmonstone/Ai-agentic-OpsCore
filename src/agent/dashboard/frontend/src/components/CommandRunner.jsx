import { useState, useEffect, useRef } from 'react'

const TYPE_LABEL = { string:'TEXT', int:'INT', float:'FLOAT', bool:'BOOL', flag:'FLAG', choice:'CHOICE' }

export default function CommandRunner({ cmd }) {
  const [values, setValues]       = useState({})
  const [output, setOutput]       = useState([])
  const [running, setRunning]     = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [error, setError]         = useState('')
  const [status, setStatus]       = useState('idle')   // idle | running | done | failed
  const wsRef     = useRef(null)
  const bottomRef = useRef(null)

  // reset when command changes
  useEffect(() => {
    setValues({}); setOutput([]); setRunning(false)
    setError(''); setConfirmed(false); setStatus('idle')
    wsRef.current?.close()
  }, [cmd?.full_command])

  // auto-scroll output
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [output])

  const set = (name, val) => setValues(p => ({ ...p, [name]: val }))

  const run = async () => {
    setError('')
    if (cmd.is_destructive && !confirmed) {
      setError('Check the confirmation box first.')
      return
    }
    setOutput([])
    setRunning(true)
    setStatus('running')

    // build params, skip empty optionals
    const params = {}
    for (const p of (cmd.params || [])) {
      const v = values[p.name]
      if (v !== undefined && v !== '') params[p.name] = v
    }

    let exec_id = null

    try {
      const res = await fetch('/api/execute', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ full_command: cmd.full_command, params, confirmed }),
      })
      const data = await res.json()

      if (!res.ok) {
        setError(data.detail || `Server error ${res.status}`)
        setRunning(false)
        setStatus('failed')
        return
      }

      exec_id = data.exec_id
    } catch (e) {
      setError(`Network error: ${e}`)
      setRunning(false)
      setStatus('failed')
      return
    }

    // Open WebSocket to stream output
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws/execution/${exec_id}`)
    wsRef.current = ws

    ws.onopen = () => {
      // WS open — buffered messages will be drained by server automatically
    }

    ws.onmessage = e => {
      let msg
      try { msg = JSON.parse(e.data) } catch { return }

      if (msg.type === 'line') {
        const text = msg.data ?? msg.text ?? ''
        if (text) setOutput(p => [...p, { kind: 'out', text }])
      } else if (msg.type === 'done') {
        const ok = (msg.exit_code ?? 0) === 0
        setOutput(p => [...p, { kind: 'done', text: ok ? 'COMPLETED ✓' : `FAILED (exit ${msg.exit_code})` }])
        setRunning(false)
        setStatus(ok ? 'done' : 'failed')
        ws.close()
      } else if (msg.type === 'error') {
        const text = msg.message ?? msg.text ?? String(msg)
        setOutput(p => [...p, { kind: 'err', text }])
        setRunning(false)
        setStatus('failed')
        ws.close()
      }
    }

    ws.onerror = () => {
      setOutput(p => [...p, { kind: 'err', text: 'WebSocket connection error' }])
      setRunning(false)
      setStatus('failed')
    }

    ws.onclose = () => {
      if (running) setRunning(false)
    }
  }

  const params       = cmd.params || []
  const requiredMiss = params.filter(p => p.required && !values[p.name])
  const canRun       = !running && requiredMiss.length === 0

  const statusColor = { idle:'#858585', running:'#f59e0b', done:'#22c55e', failed:'#ef4444' }[status]
  const statusIcon  = { idle:'', running:'◉', done:'✓', failed:'✗' }[status]

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:18 }}>

      {/* description */}
      {cmd.help_text && (
        <div style={{ fontSize:15, color:'#b0b0b0', lineHeight:1.7, padding:'12px 16px', background:'#0d0d0d', borderLeft:'2px solid #1e1e1e' }}>
          {cmd.help_text}
        </div>
      )}

      {/* destructive warning */}
      {cmd.is_destructive && (
        <div style={{ padding:'14px 16px', background:'#f59e0b08', border:'1px solid #f59e0b30', display:'flex', gap:12, alignItems:'flex-start' }}>
          <span style={{ color:'#f59e0b', fontSize:20, flexShrink:0, lineHeight:1 }}>⚠</span>
          <div>
            <div style={{ fontSize:14, color:'#f59e0b', marginBottom:10, fontWeight:600 }}>Destructive — this cannot be undone</div>
            <label style={{ display:'flex', alignItems:'center', gap:8, cursor:'pointer' }}>
              <input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}
                style={{ accentColor:'#f59e0b', width:16, height:16 }} />
              <span style={{ fontSize:14, color:'#c8c8c8' }}>I understand and want to proceed</span>
            </label>
          </div>
        </div>
      )}

      {/* params */}
      {params.length > 0 && (
        <div style={{ display:'flex', flexDirection:'column', gap:16 }}>
          <div style={{ fontSize:12, letterSpacing:2, color:'#858585' }}>PARAMETERS</div>
          {params.map(p => (
            <ParamField key={p.name} param={p} value={values[p.name]} onChange={v => set(p.name, v)} />
          ))}
        </div>
      )}

      {/* run button row */}
      <div style={{ display:'flex', alignItems:'center', gap:14, flexWrap:'wrap' }}>
        <button onClick={run} disabled={!canRun} style={{
          background: canRun ? '#e05020' : '#181818',
          border: 'none',
          color: canRun ? '#fff' : '#555',
          padding: '11px 34px',
          cursor: canRun ? 'pointer' : 'not-allowed',
          fontSize: 14, letterSpacing: 2, fontFamily: 'inherit',
          transition: 'all .15s', flexShrink: 0,
        }}
          onMouseEnter={e => { if (canRun) e.currentTarget.style.background = '#c94018' }}
          onMouseLeave={e => { if (canRun) e.currentTarget.style.background = '#e05020' }}>
          {running ? '◉  RUNNING…' : '▶  RUN'}
        </button>

        {status !== 'idle' && (
          <span style={{ fontSize:14, color: statusColor, display:'flex', alignItems:'center', gap:6 }}>
            {running && <span style={{ animation:'spin 1s linear infinite', display:'inline-block' }}>⟳</span>}
            <span>{statusIcon} {status.toUpperCase()}</span>
          </span>
        )}

        {requiredMiss.length > 0 && !running && (
          <span style={{ fontSize:13, color:'#a6a6a6' }}>
            fill in: {requiredMiss.map(p => <strong key={p.name} style={{ color:'#e05020' }}>{p.name}</strong>).reduce((a,b) => [a,', ',b])}
          </span>
        )}
        {error && <span style={{ fontSize:14, color:'#ef4444' }}>{error}</span>}
      </div>

      {/* output pane */}
      {(output.length > 0 || running) && (
        <div style={{ background:'#090909', border:'1px solid #1a1a1a', borderRadius:1 }}>
          <div style={{ padding:'10px 16px', borderBottom:'1px solid #141414', display:'flex', alignItems:'center', justifyContent:'space-between' }}>
            <span style={{ fontSize:12, letterSpacing:2, color:'#858585' }}>OUTPUT</span>
            {running && (
              <span style={{ fontSize:12, color:'#f59e0b', display:'flex', alignItems:'center', gap:6 }}>
                <span style={{ animation:'pulse 1s ease-in-out infinite', display:'inline-block',
                               width:6, height:6, borderRadius:'50%', background:'#f59e0b' }} />
                LIVE
              </span>
            )}
            {!running && output.length > 0 && (
              <button onClick={() => { setOutput([]); setStatus('idle') }} style={{
                background:'transparent', border:'none', color:'#858585', cursor:'pointer', fontSize:12
              }}>clear</button>
            )}
          </div>

          <div style={{ padding:'14px 16px', maxHeight:380, overflow:'auto', display:'flex', flexDirection:'column', gap:2 }}>
            {running && output.length === 0 && (
              <div style={{ display:'flex', alignItems:'center', gap:10, color:'#9a9a9a', fontSize:14 }}>
                <span style={{ animation:'spin 1s linear infinite', display:'inline-block' }}>⟳</span>
                <span>Waiting for output…</span>
              </div>
            )}
            {output.map((line, i) => (
              <div key={i} style={{
                fontSize: 14, lineHeight: 1.8,
                color: line.kind === 'err'  ? '#ef4444' :
                       line.kind === 'done' ? (line.text.includes('✓') ? '#22c55e' : '#ef4444') :
                       '#d0d0d0',
                whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                fontWeight: line.kind === 'done' ? 600 : 400,
                borderTop: line.kind === 'done' ? '1px solid #1a1a1a' : 'none',
                paddingTop: line.kind === 'done' ? 10 : 0,
                marginTop: line.kind === 'done' ? 8 : 0,
              }}>
                {line.kind === 'out' && <span style={{ color:'#757575', marginRight:8, userSelect:'none' }}>›</span>}
                {line.text}
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        </div>
      )}

      <style>{`
        @keyframes spin  { to { transform: rotate(360deg) } }
        @keyframes pulse { 0%,100% { opacity:.3 } 50% { opacity:1 } }
      `}</style>
    </div>
  )
}

function ParamField({ param, value, onChange }) {
  const isFlag = param.type === 'flag' || param.type === 'bool'

  if (isFlag) {
    return (
      <label style={{ display:'flex', alignItems:'center', gap:10, cursor:'pointer' }}>
        <input type="checkbox" checked={!!value} onChange={e => onChange(e.target.checked)}
          style={{ accentColor:'#e05020', width:16, height:16 }} />
        <span style={{ fontSize:14, color:'#c8c8c8' }}>{param.name}</span>
        {param.help_text && <span style={{ fontSize:13, color:'#858585' }}>— {param.help_text}</span>}
      </label>
    )
  }

  return (
    <div>
      <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:7 }}>
        <span style={{ fontSize:14, color: param.required ? '#e05020' : '#d0d0d0' }}>{param.name}</span>
        <span style={{ fontSize:11, color:'#858585', letterSpacing:1 }}>{TYPE_LABEL[param.type] || 'TEXT'}</span>
        {param.required && <span style={{ fontSize:12, color:'#e05020' }}>*</span>}
      </div>
      {param.help_text && (
        <div style={{ fontSize:13, color:'#858585', marginBottom:7, lineHeight:1.5 }}>{param.help_text}</div>
      )}
      <input
        type={param.type === 'int' || param.type === 'float' ? 'number' : 'text'}
        value={value ?? (param.default ?? '')}
        onChange={e => onChange(e.target.value)}
        placeholder={param.default != null ? String(param.default) : `Enter ${param.name}…`}
        style={{
          width: '100%', background: '#0d0d0d', border: '1px solid #1e1e1e', color: '#e8e8e8',
          padding: '10px 12px', fontSize: 14, fontFamily: 'inherit', outline: 'none',
          boxSizing: 'border-box', transition: 'border-color .15s',
        }}
        onFocus={e => e.target.style.borderColor = '#e05020'}
        onBlur={e => e.target.style.borderColor = '#1e1e1e'}
      />
    </div>
  )
}
