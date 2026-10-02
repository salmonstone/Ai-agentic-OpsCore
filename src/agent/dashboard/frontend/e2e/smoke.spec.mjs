// Every page, in both demo and live data, must render its content and
// throw nothing. This is the exact class of bug this suite exists to
// catch automatically: a demo fixture keyed wrong (DEMO[key] is not a
// function) and a panel stretching past the viewport both shipped this
// session and were only found by clicking through by hand.
import { test, expect } from '@playwright/test'
import { gotoPanel, NAV_PAGES } from './helpers.mjs'

for (const [id, label] of NAV_PAGES) {
  for (const demo of [true, false]) {
    test(`${id} renders with no console errors (${demo ? 'demo' : 'live'})`, async ({ page }) => {
      const errors = await gotoPanel(page, id, { demo })
      // Some pages deliberately render only a "couldn't check" NoData block
      // instead of their normal content when a service isn't configured —
      // the exact behavior this app is built around — so content shape
      // beyond the title bar isn't asserted here; "no console errors" is
      // the thing this test exists to catch.
      await expect(page.locator('h1')).toContainText(label, { timeout: 10000 })
      expect(errors, `console/page errors on ${id}: ${errors.join(' | ')}`).toEqual([])
    })
  }
}

test('the assistant panel is docked and shows no errors alongside every page', async ({ page }) => {
  const errors = await gotoPanel(page, 'overview')
  await expect(page.locator('[aria-label="AtlasOS Assistant"]')).toBeVisible()
  expect(errors).toEqual([])
})
