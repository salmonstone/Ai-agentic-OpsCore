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
  spend: () => {
    const base = [12.1, 12.4, 12.8, 13.1, 12.6, 12.2, 11.9, 12.3, 12.7, 13.0, 12.5, 12.4, 12.9, 13.2, 12.6,
      12.1, 12.0, 12.8, 21.7, 13.1, 12.7, 12.4, 12.6, 12.9, 13.3, 12.8, 12.6, 13.9, 13.4, 13.1]
    const daily = base.map((amount, i) => ({ date: new Date(Date.now() - (30 - i) * D).toISOString().slice(0, 10), amount }))
    return { daily, mean: 12.9, anomalies: [daily[18].date], generated_at: iso(20 * M) }
  },
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
