// Cross-service incident timeline: correlates deploys, auto-heals and every
// skill's activity into one causal chain via the same IncidentCorrelationSkill
// `agent incident correlate` uses. Mocks the endpoint — a real run calls
// Claude and can open an incident, neither of which belongs in e2e.
import { test, expect } from '@playwright/test'
import { gotoPanel, preparePage } from './helpers.mjs'

test('demo mode disables Correlate — a confident result would open a real incident', async ({ page }) => {
  const errors = await gotoPanel(page, 'incidents', { demo: true })
  await expect(page.getByRole('button', { name: 'Correlate' })).toBeDisabled()
  await expect(page.getByText('Disabled in demo mode')).toBeVisible()
  expect(errors).toEqual([])
})

test('an incident-confirming correlation shows root cause, factors and timeline', async ({ page }) => {
  const errors = await preparePage(page, { demo: false })
  await page.route('**/api/incidents', route => route.fulfill({ json: [] }))
  await page.route('**/api/slos', route => route.fulfill({ json: [] }))
  let sentMinutes = null
  await page.route('**/api/incidents/correlate', route => {
    sentMinutes = route.request().postDataJSON().minutes
    route.fulfill({ json: {
      window_minutes: sentMinutes, has_signal: true, is_incident: true, confidence: 'high',
      title: 'api crashlooping after deploy', root_cause: 'bad image tag shipped in api:v2',
      contributing_factors: ['no readiness probe configured'], primary_service: 'api', namespace: 'prod',
      severity: 'critical', incident_id: 'inc-abc12345', signal_counts: { deploys: 1, daemon_actions: 2 },
      summary: 'api crashlooping after deploy',
      timeline: [
        { timestamp: new Date(Date.now() - 600000).toISOString(), source: 'correlation', event_type: 'deploy', detail: 'api:v1 -> api:v2' },
        { timestamp: new Date(Date.now() - 300000).toISOString(), source: 'correlation', event_type: 'pod_restart', detail: 'CrashLoopBackOff x4' },
      ],
    } })
  })
  await page.goto('/#incidents')
  await page.waitForLoadState('networkidle')

  await page.getByRole('combobox').selectOption('60')
  await page.getByRole('button', { name: 'Correlate' }).click()
  await expect(page.getByText('api crashlooping after deploy')).toBeVisible()
  await expect(page.getByText(/bad image tag/)).toBeVisible()
  await expect(page.getByText('no readiness probe configured')).toBeVisible()
  await expect(page.getByText('high confidence')).toBeVisible()
  await expect(page.getByText('inc-abc1', { exact: true })).toBeVisible()
  await expect(page.getByText('pod restart')).toBeVisible()
  expect(sentMinutes).toBe(60)
  expect(errors).toEqual([])
})

test('no signal in the window shows the reassuring empty state', async ({ page }) => {
  const errors = await preparePage(page, { demo: false })
  await page.route('**/api/incidents', route => route.fulfill({ json: [] }))
  await page.route('**/api/slos', route => route.fulfill({ json: [] }))
  await page.route('**/api/incidents/correlate', route => route.fulfill({ json: {
    window_minutes: 30, has_signal: false, is_incident: false, confidence: 'low',
    summary: 'No signals from any source in the last 30 minutes — nothing to correlate.',
    timeline: [], signal_counts: { deploys: 0 },
  } }))
  await page.goto('/#incidents')
  await page.waitForLoadState('networkidle')

  await page.getByRole('button', { name: 'Correlate' }).click()
  await expect(page.getByText('Nothing to correlate', { exact: true })).toBeVisible()
  expect(errors).toEqual([])
})
