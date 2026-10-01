import { useCallback, useEffect, useRef, useState } from 'react'

/** A 401 means the dashboard login (DASHBOARD_TOKEN) is on and we're not
 *  logged in — App listens for this event and shows the login screen. */
function checkAuth(r) {
  if (r.status === 401) window.dispatchEvent(new Event('atlas-auth-required'))
}

export async function getJSON(url) {
  const r = await fetch(url)
  checkAuth(r)
  const body = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(body.error || body.detail || `${r.status} ${r.statusText}`)
  return body
}

export async function postJSON(url, data) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data ?? {}) })
  checkAuth(r)
  const body = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(body.error || body.detail || `${r.status} ${r.statusText}`)
  return body
}

/** Fetch `url` on mount, every `intervalMs` (0 = never), and on reload().
 *  Keeps the last good data while refreshing; `error` is set on failure.
 *  url = null fetches nothing (demo mode supplies the data instead). */
export function usePoll(url, intervalMs = 0) {
  const [state, setState] = useState({ data: null, error: null, loading: !!url, at: null })
  const alive = useRef(true)
  const load = useCallback(async (u = url) => {
    if (!u) return
    setState(s => ({ ...s, loading: true }))
    try {
      const data = await getJSON(u)
      if (alive.current) setState({ data, error: null, loading: false, at: Date.now() })
    } catch (e) {
      if (alive.current) setState(s => ({ ...s, error: e.message, loading: false, at: Date.now() }))
    }
  }, [url])
  useEffect(() => {
    alive.current = true
    if (!url) return () => { alive.current = false }
    load()
    const t = intervalMs ? setInterval(() => load(), intervalMs) : null
    return () => { alive.current = false; if (t) clearInterval(t) }
  }, [load, intervalMs, url])
  return { ...state, reload: load }
}

/** POST and read a Server-Sent Events stream, calling onEvent(obj) per event. */
export async function streamSSE(url, data, onEvent, signal) {
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data), signal,
  })
  checkAuth(r)
  if (!r.ok || !r.body) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.detail || `${r.status} ${r.statusText}`)
  }
  const reader = r.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i)
      buf = buf.slice(i + 2)
      for (const line of chunk.split('\n')) {
        if (line.startsWith('data: ')) {
          try { onEvent(JSON.parse(line.slice(6))) } catch { /* ignore malformed */ }
        }
      }
    }
  }
}

/** Run a discovered CLI command through /api/execute and stream its output.
 *  onLine(text) per line; resolves with the exit code. */
export async function runCommand(fullCommand, params, confirmed, onLine) {
  const { exec_id } = await postJSON('/api/execute', { full_command: fullCommand, params, confirmed })
  return new Promise(resolve => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws/execution/${exec_id}`)
    let finished = false
    ws.onmessage = e => {
      try {
        const m = JSON.parse(e.data)
        if (m.type === 'line') onLine(m.data)
        if (m.type === 'done') { finished = true; ws.close(); resolve(m.exit_code) }
      } catch { /* ignore */ }
    }
    ws.onerror = () => { if (!finished) { onLine('✗ lost connection to the dashboard server'); resolve(1) } }
    ws.onclose = () => { if (!finished) resolve(1) }
  })
}

export function ago(ts) {
  if (!ts) return ''
  const t = typeof ts === 'number' ? ts : Date.parse(ts)
  if (Number.isNaN(t)) return ''
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export function dur(ms) {
  if (ms == null) return '—'
  const s = Math.round(ms / 1000)
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
}
