// End-to-end create/delete of an SLO — the feature that used to be a dead
// "create one with `agent slo create`" message on this page. Runs in live
// (non-demo) mode, against the isolated throwaway backend from
// global-setup, so it's safe to actually write data.
import { test, expect } from '@playwright/test'
import { gotoPanel } from './helpers.mjs'

test('creating and deleting an SLO works end to end', async ({ page }) => {
  const errors = await gotoPanel(page, 'incidents', { demo: false })

  await page.getByRole('button', { name: 'New SLO' }).click()
  await page.locator('#slo-name').fill('E2E checkout availability')
  await page.locator('#slo-svc').fill('checkout')
  await page.getByRole('button', { name: 'Create SLO' }).click()

  const card = page.locator('div')
    .filter({ hasText: 'E2E checkout availability' })
    .filter({ has: page.getByRole('button', { name: 'Delete' }) })
    .last()
  await expect(card).toContainText('99.9%')
  await expect(card).toContainText('checkout')
  await expect(page.getByText('incidents for that deployment now burn its budget')).toBeVisible()

  const deleteBtn = card.getByRole('button', { name: 'Delete' })
  await deleteBtn.click()
  await expect(card.getByRole('button', { name: 'Delete?' })).toBeVisible()
  await card.getByRole('button', { name: 'Delete?' }).click()

  // Not a getByText count on the name: the success message itself reads
  // "Deleted E2E checkout availability." and would still match.
  await expect(page.getByText('No SLOs yet', { exact: false })).toBeVisible()
  expect(errors).toEqual([])
})
