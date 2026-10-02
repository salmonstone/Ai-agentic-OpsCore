// The Overview "Briefing" card — what happened since the browser's last
// visit, not just the current health snapshot the Status section already
// shows below it. Demo mode exercises the fixture end to end; live mode
// mocks /api/digest directly so the "all quiet" and error-isolation states
// (which real data may never happen to produce) are actually reachable.
import { test, expect } from '@playwright/test'
import { gotoPanel, preparePage } from './helpers.mjs'

test('demo mode shows the briefing headline and stat tiles', async ({ page }) => {
  const errors = await gotoPanel(page, 'overview', { demo: true })
  const card = page.locator('section', { has: page.getByRole('heading', { name: 'Briefing' }) })
  await expect(card.getByText(/incidents? \(1 auto-resolved\)/)).toBeVisible()
  await expect(card.getByText('Incidents', { exact: true })).toBeVisible()
  await expect(card.getByText('Auto-fixes', { exact: true })).toBeVisible()
  expect(errors).toEqual([])
})

test('an all-quiet digest shows the reassuring empty state, not stat tiles', async ({ page }) => {
  const errors = await preparePage(page, { demo: false })
  await page.route('**/api/digest*', route => route.fulfill({ json: {
    since: new Date(Date.now() - 18 * 3600000).toISOString(), had_previous: true, generated_at: new Date().toISOString(),
    headline: 'All quiet — nothing new.',
    incidents: { total: 0, open: 0, resolved: 0, items: [] },
    deploys: { total: 0, failed: 0, rollbacks: 0 },
    fixes: { total: 0 },
    approvals: { pending_now: 0, new: 0 },
  } }))
  await page.goto('/#overview')
  await page.waitForLoadState('networkidle')
  await expect(page.getByText('All quiet since you were last here')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Briefing' })).toHaveCount(0)
  expect(errors).toEqual([])
})

test('a failing sub-section reports "couldn\'t check" instead of a fake zero', async ({ page }) => {
  const errors = await preparePage(page, { demo: false })
  await page.route('**/api/digest*', route => route.fulfill({ json: {
    since: new Date(Date.now() - 18 * 3600000).toISOString(), had_previous: true, generated_at: new Date().toISOString(),
    headline: '2 deploys.',
    incidents: { error: 'incident db is locked' },
    deploys: { total: 2, failed: 0, rollbacks: 0 },
    fixes: { total: 0 },
    approvals: { pending_now: 0, new: 0 },
  } }))
  await page.goto('/#overview')
  await page.waitForLoadState('networkidle')
  const card = page.locator('section', { has: page.getByRole('heading', { name: 'Briefing' }) })
  await expect(card.getByText("couldn't check")).toBeVisible()
  expect(errors).toEqual([])
})
