// Approving/rejecting right from the notification bell — no detour through
// the Approvals page. Mocks /api/notifications so this never touches the
// real notification store, and asserts the exact approval id the backend
// recorded the request under is what gets POSTed. Routes must be registered
// before navigation — the inbox fetches on mount, and a route added after
// gotoPanel() misses that first (real, empty) response entirely.
//
// Everything is scoped to the notifications dialog: the same
// /api/notifications endpoint (just a different `limit`) also feeds
// Overview's Live Operations Feed, so an unscoped page-wide locator matches
// both and trips Playwright's strict mode.
import { test, expect } from '@playwright/test'
import { preparePage } from './helpers.mjs'

test('approving a notification calls the approvals endpoint and shows the outcome inline', async ({ page }) => {
  const errors = await preparePage(page, { demo: false })
  await page.route('**/api/notifications?*', route => route.fulfill({ json: {
    unread: 1,
    items: [{ id: 'n1', created_at: new Date().toISOString(), severity: 'warning', kind: 'approval',
      title: 'Fix waiting for your approval', message: 'Restart deployment api in namespace prod',
      meta: { approval_id: 'abc123', kind: 'k8s_apply_fix' }, read: false }],
  } }))
  let approveBody = null
  await page.route('**/api/approvals/abc123/decide', route => {
    approveBody = route.request().postDataJSON()
    route.fulfill({ json: { status: 'applied', message: 'Restarted 1/1' } })
  })
  await page.route('**/api/notifications/read', route => route.fulfill({ json: { marked: 1 } }))
  await page.goto('/#overview')
  await page.waitForLoadState('networkidle')

  await page.getByRole('button', { name: /Notifications/ }).click()
  const dialog = page.getByRole('dialog', { name: 'Notifications' })
  await expect(dialog.getByText('Fix waiting for your approval')).toBeVisible()
  await dialog.getByRole('button', { name: 'Approve', exact: true }).click()
  await expect(dialog.getByText('Approved', { exact: true })).toBeVisible()
  expect(approveBody).toEqual({ approve: true })
  expect(errors).toEqual([])
})

test('rejecting a deploy notification posts to the deploys endpoint, not approvals', async ({ page }) => {
  await preparePage(page, { demo: false })
  await page.route('**/api/notifications?*', route => route.fulfill({ json: {
    unread: 1,
    items: [{ id: 'n2', created_at: new Date().toISOString(), severity: 'warning', kind: 'approval',
      title: 'Deploy waiting for approval: api', message: 'api:v2 → prod',
      meta: { deploy_id: 'dep-9' }, read: false }],
  } }))
  let hit = false
  await page.route('**/api/deploys/dep-9/reject', route => { hit = true; route.fulfill({ json: { status: 'rejected', log: [] } }) })
  await page.route('**/api/notifications/read', route => route.fulfill({ json: { marked: 1 } }))
  await page.goto('/#overview')
  await page.waitForLoadState('networkidle')

  await page.getByRole('button', { name: /Notifications/ }).click()
  const dialog = page.getByRole('dialog', { name: 'Notifications' })
  await dialog.getByRole('button', { name: 'Reject', exact: true }).click()
  await expect(dialog.getByText('Rejected', { exact: true })).toBeVisible()
  expect(hit).toBe(true)
})
