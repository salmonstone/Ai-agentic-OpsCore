import { useState } from 'react'
import { usePoll } from '../lib/api'
import { useDemo } from '../lib/demo'
import { Icon } from './common'

function loadHidden() { try { return localStorage.getItem('atlas-setup-hidden') === '1' } catch { return false } }

/** First-run checklist on the Overview: what to connect, in order, each with
 *  a button to the page that does it. Hides itself once the required steps
 *  are done (or when dismissed). The cluster step comes from the live socket. */
export default function SetupChecklist({ live, context, onNav }) {
  const demo = useDemo()
  const setup = usePoll(demo ? null : '/api/setup', 60000)
  const [hidden, setHidden] = useState(loadHidden)
  const [expanded, setExpanded] = useState(false)
  if (demo || hidden || !setup.data) return null

  const cluster = { id: 'cluster', title: 'Connect a Kubernetes cluster', go: 'cluster',
    done: !!live && !live.offline && !!context, hint: context && live?.offline ? `${context} isn't reachable right now.` : 'Uses your kubeconfig, or add an EKS cluster from the Cluster page.' }
  const steps = [...setup.data.steps]
  steps.splice(1, 0, cluster)
  const required = steps.filter(s => !s.optional)
  const doneCount = steps.filter(s => s.done).length
  if (required.every(s => s.done) && !expanded) return null
  const next = steps.find(s => !s.done && !s.optional) || steps.find(s => !s.done)
  const shown = expanded ? steps : steps.filter(s => !s.done).slice(0, 4)
  const dismiss = () => { setHidden(true); try { localStorage.setItem('atlas-setup-hidden', '1') } catch { /* */ } }

  return (
    <section className="surface" aria-label="Set up AtlasOS" style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: 16, border: '1px solid color-mix(in srgb, var(--color-accent) 40%, transparent)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <Icon name="ph-rocket-launch" size={20} style={{ color: 'var(--color-accent)' }} />
        <div style={{ flex: '1 1 240px', minWidth: 0 }}>
          <div style={{ fontSize: 14.5, fontWeight: 500 }}>Finish setting up AtlasOS</div>
          <div className="muted" style={{ fontSize: 12 }}>{doneCount} of {steps.length} done{next ? ` — next: ${next.title.toLowerCase()}` : ''}. Everything here can be done without a terminal.</div>
        </div>
        <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => setExpanded(e => !e)}>{expanded ? 'Show less' : 'Show all steps'}</button>
        <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={dismiss} title="Hide the checklist (it won't come back on this browser)">Dismiss</button>
      </div>
      <div style={{ height: 6, borderRadius: 3, background: 'color-mix(in srgb, var(--color-text) 9%, transparent)', overflow: 'hidden' }}>
        <div style={{ height: '100%', width: `${(doneCount / steps.length) * 100}%`, background: 'var(--color-accent)', borderRadius: 3, transition: 'width .3s' }} />
      </div>
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        {shown.map(s => (
          <div key={s.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 0', background: 'var(--rule) no-repeat bottom / 100% 1px', flexWrap: 'wrap' }}>
            <Icon name={s.done ? 'ph-check-circle' : 'ph-circle'} size={18} style={{ color: s.done ? 'var(--st-ok)' : 'var(--muted)' }} />
            <div style={{ flex: '1 1 240px', minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 500, textDecoration: s.done ? 'line-through' : undefined, opacity: s.done ? 0.6 : 1 }}>
                {s.title}{s.optional && <span className="muted" style={{ fontWeight: 400, fontSize: 11.5 }}> · optional</span>}
              </div>
              {!s.done && <div className="muted" style={{ fontSize: 11.5 }}>{s.hint}</div>}
            </div>
            {!s.done && <button className={`btn ${s === next ? 'btn-primary' : 'btn-secondary'}`} style={{ fontSize: 12 }} onClick={() => onNav(s.go)}>Set up<Icon name="ph-arrow-right" /></button>}
          </div>
        ))}
      </div>
    </section>
  )
}
