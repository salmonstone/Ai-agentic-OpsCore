// Proves the boundary actually catches a crash — not just that the pattern
// looks right. Mocks /api/cluster into a shape the page doesn't guard
// against (`nodes: null`, where Cluster.jsx does `d.nodes.filter(...)`
// unguarded once `reachable` is true) to force a genuine render-time
// exception, and checks the rest of the shell survives it.
import { test, expect } from '@playwright/test'
import { gotoPanel } from './helpers.mjs'

test('a page that throws is contained — sidebar and nav keep working', async ({ page }) => {
  page.on('pageerror', () => {}) // the crash itself is expected; only assert on recovery
  await gotoPanel(page, 'overview', { demo: false })
  await page.route('**/api/cluster', route => route.fulfill({ json: {
    reachable: true, nodes: null, problem_pods: [], healed: [], metrics_available: false, context: 'test',
  } }))

  await page.getByRole('button', { name: 'Cluster', exact: true }).click()
  await expect(page.getByText('Something broke on page')).toBeVisible()
  await expect(page.getByText('The rest of the dashboard is unaffected', { exact: false })).toBeVisible()

  // The sidebar is outside the boundary and still fully usable.
  await page.getByRole('button', { name: 'Databases', exact: true }).click()
  await expect(page.locator('h1')).toHaveText('Databases')
  await expect(page.getByText('Something broke on page')).toHaveCount(0)   // fresh boundary per page
})

test('"Go to Overview" recovers from a crashed page', async ({ page }) => {
  page.on('pageerror', () => {})
  await gotoPanel(page, 'overview', { demo: false })
  await page.route('**/api/cluster', route => route.fulfill({ json: {
    reachable: true, nodes: null, problem_pods: [], healed: [], metrics_available: false, context: 'test',
  } }))
  await page.getByRole('button', { name: 'Cluster', exact: true }).click()
  await expect(page.getByText('Something broke on page')).toBeVisible()

  await page.getByRole('button', { name: 'Go to Overview' }).click()
  await expect(page.locator('h1')).toHaveText('Overview')
})
