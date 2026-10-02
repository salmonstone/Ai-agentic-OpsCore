// Minimal, safe formatter for assistant answers: paragraphs, "- " and "1. "
// lists, ``` code blocks, `inline code`, **bold**. Builds React elements —
// never injects HTML — so model output can't script the page.
import { Fragment } from 'react'

function inline(text, key) {
  const out = []
  const re = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g
  let last = 0, m, i = 0
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const t = m[0]
    out.push(t.startsWith('`')
      ? <code key={`${key}-${i++}`} style={{ fontSize: '0.9em', padding: '1px 5px', borderRadius: 4, background: 'color-mix(in srgb, var(--color-text) 9%, transparent)' }}>{t.slice(1, -1)}</code>
      : <strong key={`${key}-${i++}`} style={{ fontWeight: 600 }}>{t.slice(2, -2)}</strong>)
    last = m.index + t.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

export default function Markdown({ text }) {
  const blocks = []
  const lines = String(text).split('\n')
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (line.trimStart().startsWith('```')) {
      const body = []
      i++
      while (i < lines.length && !lines[i].trimStart().startsWith('```')) body.push(lines[i++])
      i++
      blocks.push({ kind: 'code', text: body.join('\n') })
      continue
    }
    const ul = /^\s*[-*•]\s+/.exec(line), ol = /^\s*\d+[.)]\s+/.exec(line)
    if (ul || ol) {
      const kind = ul ? 'ul' : 'ol', items = []
      while (i < lines.length && (kind === 'ul' ? /^\s*[-*•]\s+/ : /^\s*\d+[.)]\s+/).test(lines[i])) {
        items.push(lines[i].replace(kind === 'ul' ? /^\s*[-*•]\s+/ : /^\s*\d+[.)]\s+/, ''))
        i++
      }
      blocks.push({ kind, items })
      continue
    }
    if (!line.trim()) { i++; continue }
    const para = []
    while (i < lines.length && lines[i].trim() && !/^\s*([-*•]|\d+[.)])\s+/.test(lines[i]) && !lines[i].trimStart().startsWith('```')) para.push(lines[i++])
    blocks.push({ kind: 'p', text: para.join('\n') })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {blocks.map((b, k) => {
        if (b.kind === 'code') return <pre key={k} className="term" style={{ margin: 0, padding: '8px 10px', fontSize: 11.5, lineHeight: 1.5, overflowX: 'auto' }}>{b.text}</pre>
        if (b.kind === 'p') return <p key={k} style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{b.text.split('\n').map((l, j) => <Fragment key={j}>{j > 0 && <br />}{inline(l, `${k}-${j}`)}</Fragment>)}</p>
        const Tag = b.kind
        return <Tag key={k} style={{ margin: 0, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 2 }}>{b.items.map((it, j) => <li key={j}>{inline(it, `${k}-${j}`)}</li>)}</Tag>
      })}
    </div>
  )
}
