// Regression test for the panel-resize feature: dragging the handle must
// change the visible panel width (not just a wrapper that silently
// stretches past the viewport — the exact bug the first version of this
// shipped with), the width must survive a reload, and double-click must
// reset it.
import { test, expect } from '@playwright/test'
import { gotoPanel } from './helpers.mjs'

test('dragging the resize handle widens the panel and the width persists', async ({ page }) => {
  await gotoPanel(page, 'overview')
  const panel = page.locator('[aria-label="AtlasOS Assistant"]')
  const handle = page.locator('.chat-resize-handle')
  await expect(panel).toBeVisible()

  const before = (await panel.boundingBox()).width
  const box = await handle.boundingBox()
  expect(box, 'resize handle should have a real position in the viewport').not.toBeNull()
  expect(box.y).toBeGreaterThanOrEqual(0)

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width / 2 - 150, box.y + box.height / 2, { steps: 10 })
  await page.mouse.up()

  const after = (await panel.boundingBox()).width
  expect(after).toBeGreaterThan(before + 100)

  const stored = await page.evaluate(() => localStorage.getItem('atlas-chat-width'))
  expect(Number(stored)).toBeCloseTo(after, -1)

  await page.reload()
  await page.waitForLoadState('networkidle')
  const afterReload = (await panel.boundingBox()).width
  expect(afterReload).toBeCloseTo(after, 0)
})

test('double-clicking the handle resets the panel to its default width', async ({ page }) => {
  await gotoPanel(page, 'overview')
  const handle = page.locator('.chat-resize-handle')
  const panel = page.locator('[aria-label="AtlasOS Assistant"]')
  const box = await handle.boundingBox()

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width / 2 - 150, box.y + box.height / 2, { steps: 10 })
  await page.mouse.up()
  expect((await panel.boundingBox()).width).toBeGreaterThan(box.width + 100)

  await handle.dblclick()
  await expect(async () => {
    expect((await panel.boundingBox()).width).toBeCloseTo(388, 0)
  }).toPass({ timeout: 2000 })
})
