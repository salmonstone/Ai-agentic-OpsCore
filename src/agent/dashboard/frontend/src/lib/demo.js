// Demo mode: realistic SAMPLE data for showing the dashboard when there's no
// live pipeline. Always shown under a "Demo data" banner, and every action
// (Approve, Run, Start Daemon, Connect) is disabled while it's on — so it can
// never be mistaken for, or act on, real systems.
import { createContext, useContext, useMemo } from 'react'
import { usePoll } from './api'

export const DemoCtx = createContext(false)
export const useDemo = () => useContext(DemoCtx)

const iso = msAgo => new Date(Date.now() - msAgo).toISOString()
const M = 60000, H = 3600000, D = 86400000

const DEMO = {
  live: () => ({
    offline: false, timestamp: iso(0),
    stats: { daemon_running: true, pods_healed_today: 3, cost_saved_month: 212.4, actions_30d: 97, incidents_open: 1 },
  }),
  summary: () => ({
    headline: 'Needs attention: Cluster, Jenkins', generated_at: iso(40 * 1000),
    sections: [
      { title: 'Cluster', status: 'warn', lines: ['3/3 nodes ready · 42/44 pods healthy', '`prod/api-7f9c-x2kqd` — CrashLoopBackOff (0/1 ready, 14 restarts)'] },
      { title: 'Jenkins', status: 'warn', lines: ['1 failed build(s) in the last 24h:', '❌ `infragpt` #22 (3h ago)'] },
      { title: 'GitHub Actions', status: 'ok', lines: ['No failed runs in the last 24h (`salmonstone/Ai-agentic-OpsCore`).'] },
      { title: 'AWS spend', status: 'ok', lines: ['2026-09-29: $13.40 vs $12.90/day avg (+4%)'] },
      { title: 'Waiting on you', status: 'warn', lines: ['2 fix(es) waiting for your Approve/Reject in Slack:'] },
      { title: 'Backups', status: 'ok', lines: ['Last backup 6h ago (969 KB, 4 kept)'] },
      { title: 'AtlasOS', status: 'ok', lines: ['mcp up · ngrok up · daemon on'] },
    ],
  }),
  actions: () => [
    { timestamp: iso(4 * M), category: 'pod_heal', action: 'Restarted pod', resource: 'api-7f9c-x2kqd', namespace: 'prod', success: 1, note: 'CrashLoopBackOff · 14 restarts · picked up rotated db-credentials' },
    { timestamp: iso(38 * M), category: 'resource', action: 'Raised memory limit 512Mi → 768Mi', resource: 'etl-runner', namespace: 'data', success: 1, note: 'OOMKilled 3× in 20 min' },
    { timestamp: iso(2 * H), category: 'scale', action: 'Scaled 3 → 4 replicas', resource: 'worker', namespace: 'prod', success: 1, note: 'CPU 91% sustained for 6 min' },
    { timestamp: iso(5 * H), category: 'cost', action: 'Stopped idle instance', resource: 'i-0a3f9c21', namespace: '', success: 1, note: '0.4% CPU for 7 days · saves ~$38/mo' },
    { timestamp: iso(9 * H), category: 'pod_heal', action: 'Rolled back deployment', resource: 'billing-api', namespace: 'prod', success: 1, note: 'new image failed readiness 5/5' },
    { timestamp: iso(20 * H), category: 'pod_heal', action: 'Restart attempt', resource: 'cron-sync', namespace: 'ops', success: 0, note: 'escalated to PagerDuty after 3 attempts' },
  ],
  approvals: () => ({
    pending: [
      { id: 'demo-a1', kind: 'k8s_crashloop_apply_fix', summary: 'Restart deployment api in namespace prod', status: 'pending',
        params: { pod_name: 'api-7f9c-x2kqd', namespace: 'prod', fix_kind: 'rollout_restart' }, created_at: iso(6 * M), expires_at: iso(-54 * M) },
      { id: 'demo-a2', kind: 'jenkins_apply_fix', summary: 'Jenkins infragpt #22 failed: Helm ingress conflict — suggested fix: RETRIGGER', status: 'pending',
        params: { job_name: 'infragpt', build_number: 22 }, created_at: iso(14 * M), expires_at: iso(-46 * M) },
    ],
    history: [
      { id: 'demo-h1', kind: 'k8s_apply_fix', summary: 'Restart pod etl-runner-6c4f9 in data', status: 'applied', decided_at: iso(3 * H), decided_by: 'aditya (Slack)', result: { message: 'Running 1/1 after 12s' }, created_at: iso(4 * H) },
      { id: 'demo-h2', kind: 'tls_apply_fix', summary: 'Renew certificate for grafana.internal', status: 'rejected', decided_at: iso(D), decided_by: 'dashboard', result: null, created_at: iso(D + H) },
      { id: 'demo-h3', kind: 'cost_apply_fix', summary: 'Delete 3 unattached EBS volumes', status: 'expired', decided_at: iso(2 * D), decided_by: 'system', result: null, created_at: iso(2 * D + H) },
    ],
  }),
  deploys: () => [],
  clusters: () => ({
    current: 'eks-prod-ap-south-1', health_checked: true,
    clusters: [
      { name: 'eks-prod-ap-south-1', health: 'healthy', node_count: 3 },
      { name: 'eks-staging-ap-south-1', health: 'healthy', node_count: 2 },
      { name: 'kind-local', health: 'unreachable', node_count: 0 },
    ],
  }),
  cluster: () => ({
    context: 'eks-prod-ap-south-1', reachable: true, metrics_available: true, error: null, checked_at: iso(12 * 1000),
    nodes: [
      { name: 'ip-10-0-12-41', status: 'Ready', instance_type: 't3.large', age: '41d', cpu_percent: 64, memory_percent: 71 },
      { name: 'ip-10-0-14-7', status: 'Ready', instance_type: 't3.large', age: '41d', cpu_percent: 38, memory_percent: 52 },
      { name: 'ip-10-0-31-190', status: 'Ready', instance_type: 't3.xlarge', age: '9d', cpu_percent: 91, memory_percent: 68 },
    ],
    problem_pods: [
      { namespace: 'prod', name: 'api-7f9c-x2kqd', status: 'CrashLoopBackOff', ready: '0/1', restarts: 14, age: '2h' },
      { namespace: 'data', name: 'etl-runner-6c4f9-pq8zt', status: 'Pending', ready: '0/1', restarts: 0, age: '18m' },
    ],
    healed: [
      { timestamp: iso(4 * M), resource: 'api-7f9c-x2kqd', namespace: 'prod', action: 'restarted pod', note: 'CrashLoopBackOff' },
      { timestamp: iso(38 * M), resource: 'etl-runner', namespace: 'data', action: 'memory 512Mi → 768Mi', note: 'OOMKilled' },
    ],
  }),
  jenkins: () => {
    const trend = s => s.split('').map(c => ({ S: 'SUCCESS', F: 'FAILURE', U: 'UNSTABLE', R: 'RUNNING' }[c]))
    return {
      configured: true, connected: true, url: 'http://jenkins.internal:8080',
      failures: [
        { job: 'infragpt', number: 22, timestamp: Date.now() - 3 * H, duration_ms: 252000, node: 'agent-2' },
        { job: 'infragpt', number: 18, timestamp: Date.now() - 3 * D, duration_ms: 198000, node: 'agent-1' },
      ],
      jobs: [
        { name: 'infragpt', last_number: 22, status: 'FAILURE', duration_ms: 252000, timestamp: Date.now() - 3 * H, trend: trend('SSSFSSSSSF') },
        { name: 'billing-api', last_number: 109, status: 'SUCCESS', duration_ms: 362000, timestamp: Date.now() - 42 * M, trend: trend('SSSSSSSSSS') },
        { name: 'docs-site', last_number: 41, status: 'SUCCESS', duration_ms: 51000, timestamp: Date.now() - 5 * H, trend: trend('SSUSSSSSSS') },
        { name: 'etl-nightly', last_number: 311, status: 'RUNNING', duration_ms: 720000, timestamp: Date.now() - 12 * M, trend: trend('SSSSSSSSSR') },
        { name: 'atlas-os-hook-test', last_number: 1, status: 'FAILURE', duration_ms: 4000, timestamp: Date.now() - 2 * D, trend: trend('F') },
      ],
    }
  },
  incidents: () => [
    { id: 'INC-142', title: 'api pods crashlooping after db-credentials rotation', severity: 'critical', status: 'open', service: 'api', namespace: 'prod', opened_at: iso(2 * H) },
    { id: 'INC-140', title: 'etl-nightly exceeded memory', severity: 'warning', status: 'resolved', service: 'etl', namespace: 'data', opened_at: iso(3 * D) },
  ],
  slos: () => [
    { id: 's1', name: 'API availability', service: 'api', target_pct: 99.9, window_days: 30, budget_minutes: 43.2, used_minutes: 29.8, budget_pct: 31, status: 'warning' },
    { id: 's2', name: 'Checkout latency p95 < 400ms', service: 'billing-api', target_pct: 99.5, window_days: 30, budget_minutes: 216, used_minutes: 30, budget_pct: 86, status: 'healthy' },
    { id: 's3', name: 'ETL completes by 06:00', service: 'etl', target_pct: 99, window_days: 30, budget_minutes: 432, used_minutes: 410, budget_pct: 5, status: 'critical' },
  ],
  chart: () => {
    const labels = [], data = []
    const pattern = [2, 3, 1, 4, 2, 0, 1, 3, 5, 2, 1, 2, 6, 3, 2, 1, 0, 2, 4, 3, 2, 8, 3, 2, 1, 3, 2, 4, 3, 3]
    for (let i = 29; i >= 0; i--) {
      labels.push(new Date(Date.now() - i * D).toISOString().slice(5, 10))
      data.push(pattern[29 - i])
    }
    return { labels, data }
  },
  notifications: () => ({
    unread: 3,
    items: [
      { id: 'n1', created_at: iso(4 * M), severity: 'warning', kind: 'approval', title: 'Fix waiting for your approval', message: 'Restart deployment api in namespace prod', meta: {}, read: false },
      { id: 'n2', created_at: iso(6 * M), severity: 'critical', kind: 'alert', title: 'CrashLoopBackOff: prod/api-7f9c-x2kqd', message: '14 restarts in 2h — exits with "password authentication failed"', meta: { Namespace: 'prod', Resource: 'api-7f9c-x2kqd' }, read: false },
      { id: 'n3', created_at: iso(3 * H), severity: 'warning', kind: 'alert', title: 'Jenkins build failed', message: 'infragpt #22 failed. Cause: Helm ingress conflict. Needs a manual fix.', meta: {}, read: false },
      { id: 'n4', created_at: iso(5 * H), severity: 'ok', kind: 'resolved', title: 'Recovered: data/etl-runner', message: '', meta: { Namespace: 'data' }, read: true },
      { id: 'n5', created_at: iso(9 * H), severity: 'info', kind: 'summary', title: '☀️ AtlasOS daily summary — Wed 01 Oct', message: '', meta: {}, read: true },
    ],
  }),
  github: () => {
    const r = (id, n, wf, title, branch, conclusion, ago_, status = 'completed') => ({ id, run_number: n, workflow: wf, title, branch, event: 'push', status, conclusion, sha: Math.random().toString(16).slice(2, 9), created_at: iso(ago_), url: '' })
    return {
      configured: true, repo: 'salmonstone/Ai-agentic-OpsCore',
      runs: [
        r(9101, 212, 'CI', 'feat(dashboard): settings & logs', 'feat/dashboard-redesign', null, 3 * M, 'in_progress'),
        r(9100, 211, 'CI', 'fix(dashboard): token without restart', 'feat/dashboard-redesign', 'success', 40 * M),
        r(9099, 210, 'Docker', 'fix(dashboard): token without restart', 'feat/dashboard-redesign', 'success', 41 * M),
        r(9098, 209, 'CI', 'wip: flaky retry test', 'tmp/retry', 'failure', 5 * H),
        r(9097, 208, 'CI', 'feat(summary): daily Slack summary', 'main', 'success', 20 * H),
        r(9096, 207, 'Docker', 'feat(summary): daily Slack summary', 'main', 'success', 20 * H),
        r(9095, 206, 'CI', 'feat(github): diagnose failed runs', 'main', 'success', 2 * D),
        r(9094, 205, 'CI', 'chore: bump deps', 'deps/bump', 'cancelled', 3 * D),
      ],
    }
  },
  aws: () => {
    const daily = DEMO.spend().daily
    return {
      reachable: true, region: 'ap-south-1', generated_at: iso(12 * M),
      identity: { arn: 'arn:aws:iam::123456789012:user/demo', account: '123456789012' },
      cost: {
        total: 390.1, month_to_date: 13.4, forecast: 402.0, last_month: 371.2, month_change_pct: 5,
        by_service: { 'Amazon Elastic Compute Cloud - Compute': 186.2, 'Amazon Elastic Kubernetes Service': 72.0, 'EC2 - Other': 48.3, 'Amazon Relational Database Service': 39.5, 'Amazon Simple Storage Service': 14.1, 'Elastic Load Balancing': 18.6, 'AmazonCloudWatch': 7.2, 'Amazon Route 53': 1.5, 'AWS Key Management Service': 1.0 },
        daily,
      },
      inventory: {
        ec2: [
          { id: 'i-0a3f9c21', name: 'eks-node-1', instance_type: 't3.large', state: 'running', private_ip: '10.0.12.41', public_ip: '', availability_zone: 'ap-south-1a' },
          { id: 'i-0b77d1e4', name: 'eks-node-2', instance_type: 't3.large', state: 'running', private_ip: '10.0.14.7', public_ip: '', availability_zone: 'ap-south-1b' },
          { id: 'i-0c19aa03', name: 'jenkins', instance_type: 't3.medium', state: 'running', private_ip: '10.0.3.9', public_ip: '13.232.0.10', availability_zone: 'ap-south-1a' },
          { id: 'i-0d55e2b8', name: 'old-bastion', instance_type: 't2.micro', state: 'stopped', private_ip: '10.0.1.4', public_ip: '', availability_zone: 'ap-south-1a' },
        ],
        rds: [{ id: 'prod-postgres', engine: 'postgres', engine_version: '16.3', instance_class: 'db.t4g.medium', status: 'available', is_healthy: true, allocated_storage: 100, multi_az: true }],
        load_balancers: [{ name: 'k8s-prod-ingress', type: 'application', state: 'active', is_healthy: true, healthy_targets: 3, total_targets: 3, dns_name: 'k8s-prod-ingress-123.ap-south-1.elb.amazonaws.com' }],
        elastic_ips: [
          { allocation_id: 'eipalloc-1', public_ip: '13.232.0.10', name: 'jenkins', is_attached: true, instance_id: 'i-0c19aa03' },
          { allocation_id: 'eipalloc-2', public_ip: '3.110.45.2', name: 'old-bastion-ip', is_attached: false },
        ],
      },
    }
  },
  deploys_setup: () => ({
    secret_set: true, secret_where: 'keychain', protected: true,
    urls: ['https://demo-atlas.ngrok-free.app/webhook/github'], local_url: 'http://127.0.0.1:8000/webhook/github', error: null,
    mappings: [
      { repo: 'salmonstone/billing-api', branch: 'main', deployment: 'billing-api', namespace: 'prod', image_prefix: '1234.dkr.ecr.ap-south-1.amazonaws.com/billing-api', auto_approve_low_risk: false },
      { repo: 'salmonstone/docs-site', branch: 'main', deployment: 'docs', namespace: 'web', image_prefix: '', auto_approve_low_risk: true },
    ],
    history: [
      { id: 'd1', repo: 'salmonstone/billing-api', branch: 'main', deployment: 'billing-api', namespace: 'prod', new_image: 'billing-api:4f1c2e9', risk_label: 'MEDIUM', status: 'deployed', created_at: iso(3 * H) },
      { id: 'd2', repo: 'salmonstone/docs-site', branch: 'main', deployment: 'docs', namespace: 'web', new_image: 'docs:a71b003', risk_label: 'LOW', status: 'deployed', created_at: iso(D) },
      { id: 'd3', repo: 'salmonstone/billing-api', branch: 'main', deployment: 'billing-api', namespace: 'prod', new_image: 'billing-api:9e02d1a', risk_label: 'HIGH', status: 'rejected', created_at: iso(2 * D) },
    ],
  }),
  runbooks: () => ({
    file: 'data\\runbooks.yaml',
    runbooks: [
      { id: 'disk-full', name: 'Disk Full Recovery', description: 'When PVC is almost full: clean images, expand PVC, alert team', trigger_condition: 'pvc_usage_pct > 85', vars: ['namespace', 'pvc_name', 'usage_pct', 'pod_name'],
        steps: [{ name: 'alert_start', type: 'slack', detail: 'Disk full recovery started for {namespace}/{pvc_name}', on_failure: 'escalate' }, { name: 'clean_docker_images', type: 'kubectl', detail: 'exec {pod_name} -n {namespace} -- docker image prune -f', on_failure: 'continue' }, { name: 'expand_pvc', type: 'python', detail: 'expand_pvc', on_failure: 'escalate' }] },
      { id: 'node-not-ready', name: 'Node Not Ready', description: 'Cordon and drain a NotReady node so pods reschedule', trigger_condition: 'node.status == NotReady', vars: ['node_name'],
        steps: [{ name: 'cordon', type: 'kubectl', detail: 'cordon {node_name}', on_failure: 'escalate' }, { name: 'drain', type: 'kubectl', detail: 'drain {node_name} --ignore-daemonsets', on_failure: 'escalate' }] },
    ],
    runs: [
      { id: 'r1', runbook_id: 'disk-full', trigger: 'daemon', status: 'success', steps_done: 4, steps_total: 4, started_at: iso(6 * H) },
      { id: 'r2', runbook_id: 'node-not-ready', trigger: 'manual', status: 'partial', steps_done: 1, steps_total: 2, started_at: iso(3 * D) },
    ],
  }),
  scale: () => ({
    daemon_running: true,
    policies: [
      { id: 'p1', deployment: 'worker', namespace: 'staging', schedule_down_utc: '17:30', schedule_up_utc: '03:30', down_replicas: 0, up_replicas: 2, cpu_threshold_pct: 80, max_replicas: 10, enabled: 1 },
      { id: 'p2', deployment: 'api', namespace: 'prod', schedule_down_utc: '19:00', schedule_up_utc: '02:30', down_replicas: 2, up_replicas: 4, cpu_threshold_pct: 75, max_replicas: 12, enabled: 1 },
    ],
    events: [
      { id: 'e1', deployment: 'worker', namespace: 'staging', old_replicas: 2, new_replicas: 0, reason: 'scheduled scale-down 17:30 UTC', status: 'done', created_at: iso(2 * H) },
      { id: 'e2', deployment: 'api', namespace: 'prod', old_replicas: 4, new_replicas: 6, reason: 'CPU 91% > 75%', status: 'done', created_at: iso(9 * H) },
    ],
  }),
  summary_info: () => ({
    slack: true, schedule: { available: true, schedule: 'Daily', next_run: 'tomorrow 9:00 AM', last_run: 'today 9:00 AM', status: 'Ready' },
    history: [0, 1, 2, 3].map(i => ({ id: `s${i}`, title: '☀️ AtlasOS daily summary', created_at: iso(i * D + 2 * H), kind: 'summary' })),
  }),
  domains: () => {
    const ok = { ok: true }
    return {
      reachable: true,
      results: [
        { found: true, domain: 'app.atlasdemo.dev', ingress: { name: 'web', namespace: 'prod' }, checks: { lb: ok, dns: ok, tls_section: ok, tls_cert: ok, tls_secret: ok, http: ok, https: ok, backend: ok }, diagnosis: { overall_status: 'live' } },
        { found: true, domain: 'grafana.atlasdemo.dev', ingress: { name: 'grafana', namespace: 'monitoring' }, checks: { lb: ok, dns: ok, tls_section: { ok: false, error: 'Ingress has no spec.tls' }, tls_cert: { ok: false }, tls_secret: { ok: false }, http: ok, https: { ok: false, error: 'SSL handshake failed' }, backend: ok },
          diagnosis: { overall_status: 'degraded', root_cause: 'The Ingress has no TLS section, so the controller serves its default certificate.', suggested_fix: 'Add spec.tls for grafana.atlasdemo.dev pointing at the cert-manager secret.', fix_command: 'kubectl patch ingress grafana -n monitoring --type=merge -p \'{"spec":{"tls":[{"hosts":["grafana.atlasdemo.dev"],"secretName":"grafana-tls"}]}}\'' } },
      ],
    }
  },
  databases: () => ({
    reachable: true, region: 'ap-south-1', generated_at: iso(4 * M),
    databases: [
      { instance: { id: 'prod-postgres', engine: 'postgres', class: 'db.t4g.medium', status: 'available', storage_gb: 100, storage_type: 'gp3', multi_az: true, endpoint: 'prod-postgres.abc123.ap-south-1.rds.amazonaws.com', port: 5432 },
        metrics: { cpu_pct: 41.2, connections_max: 88, free_storage_gb: 9.1, read_latency_ms: 1.4, freeable_memory_mb: 812 }, status: 'critical', incident_id: 'a81c22f0',
        issues: [{ type: 'LOW_STORAGE', severity: 'critical', detail: 'Only 9.1GB free', fix: 'Increase allocated storage or enable storage autoscaling' }] },
      { instance: { id: 'analytics-mysql', engine: 'mysql', class: 'db.t3.small', status: 'available', storage_gb: 50, storage_type: 'gp2', multi_az: false, endpoint: 'analytics.abc123.ap-south-1.rds.amazonaws.com', port: 3306 },
        metrics: { cpu_pct: 12.5, connections_max: 14, free_storage_gb: 37.8, read_latency_ms: 0.9, freeable_memory_mb: 640 }, status: 'healthy', incident_id: '', issues: [] },
    ],
  }),
  spend: () => {
    const base = [12.1, 12.4, 12.8, 13.1, 12.6, 12.2, 11.9, 12.3, 12.7, 13.0, 12.5, 12.4, 12.9, 13.2, 12.6,
      12.1, 12.0, 12.8, 21.7, 13.1, 12.7, 12.4, 12.6, 12.9, 13.3, 12.8, 12.6, 13.9, 13.4, 13.1]
    const daily = base.map((amount, i) => ({ date: new Date(Date.now() - (30 - i) * D).toISOString().slice(0, 10), amount }))
    return { daily, mean: 12.9, anomalies: [daily[18].date], generated_at: iso(20 * M) }
  },
  digest: () => ({
    since: iso(18 * H), had_previous: true, generated_at: iso(30 * 1000),
    headline: '2 incidents (1 auto-resolved), 3 deploys, 4 auto-fixes, 1 approval(s) waiting.',
    incidents: { total: 2, open: 1, resolved: 1, items: [
      { id: 'inc-9', title: 'api latency above SLO', service: 'api', namespace: 'prod', status: 'open' },
    ] },
    deploys: { total: 3, failed: 0, rollbacks: 0 },
    fixes: { total: 4 },
    approvals: { pending_now: 1, new: 1 },
  }),
}

/** Like usePoll, but returns sample data (and fetches nothing) in demo mode.
 *  Components inside the DemoCtx provider get the flag from context; the
 *  shell that *renders* the provider can't, so it passes `demoFlag`. */
export function useData(key, url, intervalMs = 0, demoFlag) {
  const ctx = useDemo()
  const demo = demoFlag ?? ctx
  const poll = usePoll(demo ? null : url, intervalMs)
  const fixture = useMemo(() => (demo ? DEMO[key]() : null), [demo, key])
  if (demo) return { data: fixture, error: null, loading: false, at: Date.now(), reload: () => {} }
  return poll
}

export const demoLive = () => DEMO.live()

export const DEMO_ACTIVITY = () => ({
  sources: { dashboard: 4, approvals: 3, 'auto-healer': 5, backup: 2, deployment: 2, 'k8s-diagnose': 40 },
  action_sources: ['dashboard', 'approvals', 'auto-healer', 'backup', 'deployment'],
  items: [
    { id: 'a1', source: 'dashboard', created_at: iso(3 * M), content: 'Dashboard: acknowledged incident INC-142' },
    { id: 'a2', source: 'auto-healer', created_at: iso(4 * M), content: 'Restarted pod prod/api-7f9c-x2kqd — CrashLoopBackOff, 14 restarts. Picked up the rotated db-credentials secret; Running 1/1 after 12s.' },
    { id: 'a3', source: 'approvals', created_at: iso(3 * H), content: 'Approval demo-h1 (k8s_apply_fix) approved by aditya (Slack) -> APPLIED: Restart pod etl-runner-6c4f9 in data' },
    { id: 'a4', source: 'deployment', created_at: iso(3 * H + 20 * M), content: 'Deployed billing-api:4f1c2e9 to prod/billing-api — risk MEDIUM, healthy after 94s' },
    { id: 'a5', source: 'dashboard', created_at: iso(5 * H), content: 'Dashboard: scaling policy for worker/staging — down 17:30 UTC to 0, up 03:30 UTC to 2' },
    { id: 'a6', source: 'backup', created_at: iso(6 * H), content: 'Backup atlasos-20261001T083914Z.tar.gz created (972 KB)' },
    { id: 'a7', source: 'dashboard', created_at: iso(D + H), content: "Dashboard: created SLO 'API availability' for api/prod (99.9% over 30d)" },
    { id: 'a8', source: 'approvals', created_at: iso(D + 2 * H), content: 'Approval demo-h2 (tls_apply_fix) rejected by dashboard: Renew certificate for grafana.internal' },
  ],
})
