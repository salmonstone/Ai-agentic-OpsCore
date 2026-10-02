import { useState } from 'react'
import { ago, delJSON, postJSON } from '../lib/api'
import { useData, useDemo } from '../lib/demo'
import { Card, ConfirmButton, CopyButton, Field, Icon, Msg, NoData, Pill, Section, SectionHead, SkeletonRows, TableCard } from './common'

const EMPTY = { repo: '', branch: 'main', deployment: '', namespace: 'default', image_prefix: '', auto_approve_low_risk: false }
const STATUS_T = { pending: 'warn', approved: 'ok', deployed: 'ok', success: 'ok', rejected: 'neutral', failed: 'crit', rolled_back: 'crit', gated: 'warn' }

function Step({ n, done, title, children }) {
  return (
    <div style={{ display: 'flex', gap: 12 }}>
      <div style={{ width: 26, height: 26, flex: 'none', borderRadius: '50%', display: 'grid', placeItems: 'center', fontSize: 12, fontWeight: 600,
        color: done ? 'var(--st-ok)' : 'var(--color-accent)', border: `1.5px solid ${done ? 'var(--st-ok)' : 'var(--color-accent)'}` }}>
        {done ? <Icon name="ph-check" /> : n}
      </div>
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div style={{ fontSize: 13.5, fontWeight: 500, paddingTop: 3 }}>{title}</div>
        {children}
      </div>
    </div>
  )
}

function Row({ label, value, mono = true }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: 12.5 }}>
      <span className="muted" style={{ minWidth: 110 }}>{label}</span>
      <code className={mono ? 'term' : undefined} style={{ padding: '4px 8px', fontSize: 12, userSelect: 'all', wordBreak: 'break-all' }}>{value}</code>
      <CopyButton text={value} />
    </div>
  )
}

export default function Deploys({ onNav }) {
  const demo = useDemo()
  const setup = useData('deploys_setup', '/api/deploys/setup', 30000)
  const [form, setForm] = useState(EMPTY)
  const [secret, setSecret] = useState(null)
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(null)
  const d = setup.data

  const act = async (key, fn) => {
    setBusy(key); setMsg(null)
    try { setMsg({ ok: true, text: await fn() }) } catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy(null); setup.reload()
  }
  const genSecret = replace => act('secret', async () => {
    const r = await postJSON('/api/deploys/secret', { replace })
    setSecret(r.secret)
    return `Secret ${replace ? 'replaced' : 'created'} and saved to your keychain. ${r.note}`
  })
  const addMapping = e => {
    e.preventDefault()
    act('map', async () => {
      const r = await postJSON('/api/deploys/mappings', form)
      setForm(EMPTY)
      return r.warnings.length ? `Saved, with warnings: ${r.warnings.join(' · ')}` : `Saved — pushes to ${form.repo}:${form.branch} now deploy ${form.deployment}.`
    })
  }
  const remove = m => act(`rm-${m.repo}-${m.branch}`, async () => {
    await delJSON(`/api/deploys/mappings?repo=${encodeURIComponent(m.repo)}&branch=${encodeURIComponent(m.branch)}`)
    return `Removed ${m.repo}:${m.branch}.`
  })

  if (!d && setup.loading) return <SkeletonRows rows={5} />
  if (!d) return <NoData label="Deploys" note="Couldn't read the deploy setup." error={setup.error} onRetry={() => setup.reload()} />

  const url = d.urls[0]
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  return (
    <div data-screen-label="Deploys" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      <div className="muted" style={{ fontSize: 12.5, maxWidth: 820 }}>
        A push to a mapped branch reaches AtlasOS through a GitHub webhook. AtlasOS scores the risk and puts the deploy in
        {' '}<a href="#approvals" onClick={e => { e.preventDefault(); onNav('approvals') }}>Approvals</a> (and Slack) — nothing rolls out until you approve it,
        unless you allow low-risk deploys to go automatically.
      </div>
      <Msg msg={msg} />

      <Section>
        <SectionHead title="Connect GitHub" note="three steps, once per repo" />
        <Card>
          <Step n={1} done={d.secret_set} title="Create the webhook secret">
            {secret ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div style={{ fontSize: 12.5, fontWeight: 500, color: 'var(--st-warn)' }}>Copy it now — it won't be shown again.</div>
                <Row label="Secret" value={secret} />
              </div>
            ) : d.secret_set ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', fontSize: 12.5 }}>
                <span className="muted">Set (saved in {d.secret_where}). Lost it? Make a new one and update GitHub too.</span>
                <ConfirmButton danger icon="ph-arrows-clockwise" confirmLabel="Replace? GitHub must be updated" busy={busy === 'secret'} disabled={demo || !d.protected} onConfirm={() => genSecret(true)}>New secret</ConfirmButton>
              </div>
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <button className="btn btn-primary" onClick={() => genSecret(false)} disabled={demo || busy === 'secret' || !d.protected}><Icon name="ph-key" />Generate secret</button>
                {!d.protected && <span className="muted" style={{ fontSize: 12 }}>Protect the dashboard first (<a href="#settings" onClick={e => { e.preventDefault(); onNav('settings') }}>Settings</a>) — it's a credential.</span>}
              </div>
            )}
          </Step>
          <Step n={2} done={d.mappings.length > 0} title="Say which repo deploys what">
            <span className="muted" style={{ fontSize: 12 }}>Add a mapping below — repo and branch → Kubernetes deployment.</span>
          </Step>
          <Step n={3} title="Add the webhook in GitHub">
            {url ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <Row label="Payload URL" value={url} />
                <div style={{ fontSize: 12.5 }}><span className="muted" style={{ display: 'inline-block', minWidth: 110 }}>Content type</span><span className="mono">application/json</span></div>
                <div style={{ fontSize: 12.5 }}><span className="muted" style={{ display: 'inline-block', minWidth: 110 }}>Secret</span>the one from step 1</div>
                <div style={{ fontSize: 12.5 }}><span className="muted" style={{ display: 'inline-block', minWidth: 110 }}>Events</span>Just the push event</div>
                <div className="muted" style={{ fontSize: 11.5 }}>
                  In GitHub: the repo → Settings → Webhooks → Add webhook.
                  {d.mappings.map(m => <span key={m.repo}> <a href={`https://github.com/${m.repo}/settings/hooks/new`} target="_blank" rel="noreferrer">{m.repo} ↗</a></span>)}
                  {' '}This URL is your ngrok tunnel; on ngrok's free plan it changes when ngrok restarts — then update it in GitHub.
                </div>
              </div>
            ) : (
              <div style={{ fontSize: 12.5, color: 'var(--st-warn)' }}>
                <Icon name="ph-warning" /> No public tunnel is running, so GitHub can't reach AtlasOS. Start AtlasOS's services (see <a href="#system" onClick={e => { e.preventDefault(); onNav('system') }}>System</a>);
                the receiver listens locally at <span className="mono">{d.local_url}</span>.
              </div>
            )}
          </Step>
        </Card>
      </Section>

      <Section>
        <SectionHead title="Mappings" note={`${d.mappings.length} configured`} />
        {d.error && <NoData note="Couldn't read the mappings file." error={d.error} />}
        {d.mappings.length > 0 && (
          <TableCard>
            <table className="table">
              <thead><tr><th>Repo</th><th>Branch</th><th>Deploys</th><th>Image prefix</th><th>Low-risk</th><th /></tr></thead>
              <tbody>
                {d.mappings.map(m => (
                  <tr key={`${m.repo}:${m.branch}`}>
                    <td className="mono" style={{ fontSize: 12 }}>{m.repo}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{m.branch}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{m.deployment}<span className="muted">/{m.namespace}</span></td>
                    <td className="mono muted" style={{ fontSize: 11.5, maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis' }}>{m.image_prefix || '—'}</td>
                    <td>{m.auto_approve_low_risk ? <Pill t="warn" label="auto-deploy" icon={false} /> : <Pill t="ok" label="needs approval" icon={false} />}</td>
                    <td style={{ textAlign: 'right' }}>
                      <ConfirmButton danger icon="ph-trash" confirmLabel="Remove?" style={{ fontSize: 12, padding: '3px 8px' }} disabled={demo} busy={busy === `rm-${m.repo}-${m.branch}`} onConfirm={() => remove(m)}>Remove</ConfirmButton>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableCard>
        )}
        <Card>
          <form onSubmit={addMapping} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ fontSize: 13.5, fontWeight: 500 }}>Add a mapping</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))', gap: 10 }}>
              <Field id="m-repo" label="GitHub repo"><input id="m-repo" className="input mono" required placeholder="owner/name" value={form.repo} onChange={e => set('repo', e.target.value.trim())} /></Field>
              <Field id="m-branch" label="Branch"><input id="m-branch" className="input mono" required value={form.branch} onChange={e => set('branch', e.target.value.trim())} /></Field>
              <Field id="m-dep" label="Kubernetes deployment"><input id="m-dep" className="input mono" required placeholder="api" value={form.deployment} onChange={e => set('deployment', e.target.value.trim())} /></Field>
              <Field id="m-ns" label="Namespace"><input id="m-ns" className="input mono" required value={form.namespace} onChange={e => set('namespace', e.target.value.trim())} /></Field>
              <Field id="m-img" label="Image prefix (optional)" hint="registry/repo without the tag"><input id="m-img" className="input mono" placeholder="1234.dkr.ecr…/api" value={form.image_prefix} onChange={e => set('image_prefix', e.target.value.trim())} /></Field>
            </div>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12.5 }}>
              <input type="checkbox" id="m-auto" checked={form.auto_approve_low_risk} onChange={e => set('auto_approve_low_risk', e.target.checked)} />
              Deploy LOW-risk changes without asking <span className="muted">(medium and high risk always wait for you)</span>
            </label>
            <div><button className="btn btn-primary" type="submit" disabled={demo || busy === 'map'}><Icon name={busy === 'map' ? 'ph-circle-notch' : 'ph-plus'} className={busy === 'map' ? 'spin' : undefined} />{busy === 'map' ? 'Checking the cluster…' : 'Add mapping'}</button></div>
          </form>
        </Card>
      </Section>

      <Section>
        <SectionHead title="Recent deploys" note="from pushes" />
        {d.history.length === 0 ? <div className="muted" style={{ fontSize: 12.5 }}>No deploys yet — they appear here after the first push to a mapped branch.</div> : (
          <TableCard>
            <table className="table">
              <thead><tr><th>When</th><th>Repo</th><th>Deployment</th><th>Image</th><th>Risk</th><th>Status</th></tr></thead>
              <tbody>
                {d.history.map(h => (
                  <tr key={h.id}>
                    <td className="muted" style={{ fontSize: 12 }}>{ago(h.created_at)}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{h.repo}<span className="muted">:{h.branch}</span></td>
                    <td className="mono" style={{ fontSize: 12 }}>{h.deployment}<span className="muted">/{h.namespace}</span></td>
                    <td className="mono muted" style={{ fontSize: 11.5, maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{(h.new_image || '').split('/').pop()}</td>
                    <td style={{ fontSize: 12 }}>{h.risk_label || '—'}</td>
                    <td><Pill t={STATUS_T[h.status] || 'neutral'} label={h.status} icon={false} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableCard>
        )}
      </Section>
    </div>
  )
}
