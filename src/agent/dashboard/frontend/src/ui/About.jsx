import { Icon, NoData, Section, SectionHead, SkeletonRows } from './common'

// Group the assistant's tools by their name prefix (k8s_, jenkins_, aws_ …).
const GROUP = {
  k8s: 'Kubernetes', jenkins: 'Jenkins', github: 'GitHub Actions', aws: 'AWS', cost: 'AWS', tls: 'TLS & domains',
  domain: 'TLS & domains', dns: 'Networking', ingress: 'Networking', security: 'Security', incident: 'Incidents',
  deploy: 'Deploys', daily: 'Reporting', approval: 'Approvals', propose: 'Approvals', list: 'Approvals',
  memory: 'AtlasOS', project: 'AtlasOS', run: 'AtlasOS',
}

export default function About({ about, chatInfo }) {
  const a = about.data
  const tools = chatInfo.data?.tools || []
  const groups = {}
  for (const t of tools) (groups[GROUP[t.name.split('_')[0]] || 'Other'] ||= []).push(t)
  const facts = a && !a.error ? [
    ['Version', a.version || '—'],
    ['Skills', String(a.skills?.length ?? '—')],
    ['Assistant tools', String(tools.length || '—')],
    ['Assistant can change things', chatInfo.data ? (chatInfo.data.mutations_enabled ? 'yes — asks first' : 'no (read-only)') : '—'],
    ...Object.entries(a.runtime || {}).filter(([, v]) => ['string', 'number', 'boolean'].includes(typeof v)).slice(0, 4)
      .map(([k, v]) => [k.replace(/_/g, ' '), String(v)]),
  ] : []

  return (
    <div data-screen-label="07 About" style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
      {!a && about.loading && <SkeletonRows rows={2} />}
      {(about.error || a?.error) && <NoData label="About" note="Couldn't scan the project." error={about.error || a?.error} />}
      {facts.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: 1, borderRadius: 'var(--radius-md)', overflow: 'hidden', background: 'var(--color-divider)', boxShadow: 'var(--shadow-sm)' }}>
          {facts.map(([k, v]) => (
            <div key={k} style={{ display: 'flex', flexDirection: 'column', gap: 3, padding: '12px 14px', background: 'var(--color-surface)' }}>
              <span className="kicker" style={{ fontSize: 10.5 }}>{k}</span>
              <span className="mono" style={{ fontSize: 12.5 }}>{v}</span>
            </div>
          ))}
        </div>
      )}

      <Section>
        <SectionHead title={`What the assistant can run · ${tools.length} tools`}>
          <span className="muted" style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11 }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--st-crit)' }} />changes things (asks you first)
          </span>
        </SectionHead>
        {chatInfo.error && <NoData note="Couldn't load the assistant's tool list." error={chatInfo.error} />}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: 10, alignItems: 'start' }}>
          {Object.entries(groups).sort().map(([g, ts]) => (
            <div key={g} className="surface" style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '12px 14px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12.5, fontWeight: 500 }}><span>{g}</span><span className="mono muted" style={{ fontSize: 11 }}>{ts.length}</span></div>
              {ts.map(t => (
                <div key={t.name} title={t.description} style={{ display: 'flex', alignItems: 'center', gap: 7, fontFamily: 'var(--font-mono)', fontSize: 11.5 }}>
                  <span style={{ flex: 1 }}>{t.name}</span>
                  {t.mutating && <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--st-crit)' }} />}
                </div>
              ))}
            </div>
          ))}
        </div>
      </Section>

      {a?.skills?.length > 0 && (
        <Section>
          <SectionHead title={`Skills · ${a.skills.length}`} note="src/agent/skills — shared by the CLI, MCP and this dashboard" />
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 10 }}>
            {a.skills.map(s => (
              <div key={s.name} className="surface" style={{ padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 4 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}><Icon name="ph-puzzle-piece" style={{ color: 'var(--color-accent)' }} /><span className="mono" style={{ fontSize: 12 }}>{s.name}</span></div>
                <div className="muted" style={{ fontSize: 11.5 }}>{(s.description || s.label || '').slice(0, 140)}</div>
              </div>
            ))}
          </div>
        </Section>
      )}
    </div>
  )
}
