// The tab title and favicon reflect pending work even when the tab isn't
// focused. A screenshot can't see the OS tab bar, so this checks the two
// things that actually drive it: document.title, and the favicon <link>'s
// href actually switching to a drawn data: URL (proving the canvas path
// ran without throwing) and back to the plain icon once clear.
import { test, expect } from '@playwright/test'
import { preparePage } from './helpers.mjs'

test('one pending approval badges the tab title and favicon; clearing it restores both', async ({ page }) => {
  const errors = await preparePage(page, { demo: false })
  await page.route('**/api/approvals', route => route.fulfill({ json: {
    pending: [{ id: 'a1', kind: 'k8s_apply_fix', summary: 'Restart deployment api', params: {}, created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 3600000).toISOString() }],
    history: [],
  } }))
  await page.route('**/api/pending-deploys', route => route.fulfill({ json: [] }))
  await page.goto('/#overview')
  await page.waitForLoadState('networkidle')

  await expect(page).toHaveTitle(/^\(1\) AtlasOS$/)
  const badgedHref = await page.locator('#favicon').getAttribute('href')
  expect(badgedHref).toMatch(/^data:image\/png;base64,/)
  // A real drawn image, not an empty/broken canvas result.
  expect(badgedHref.length).toBeGreaterThan(500)

  await page.unroute('**/api/approvals')
  await page.route('**/api/approvals', route => route.fulfill({ json: { pending: [], history: [] } }))
  await page.getByRole('button', { name: 'Refresh' }).click()   // Overview's Refresh reloads approvals immediately, no need to wait for the 30s poll
  await expect(page).toHaveTitle('AtlasOS')
  await expect(page.locator('#favicon')).toHaveAttribute('href', '/favicon.svg')
  expect(errors).toEqual([])
})
