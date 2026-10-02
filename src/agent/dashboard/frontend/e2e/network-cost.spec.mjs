// Network (on-demand scan, every call costs an LLM call so it must never
// auto-fire) and Cost & Waste (auto-polled, kubectl-only by default).
import { test, expect } from '@playwright/test'
import { gotoPanel } from './helpers.mjs'

test('Network page does not scan on load, and the demo scan renders results', async ({ page }) => {
  const errors = await gotoPanel(page, 'network', { demo: true })
  await expect(page.getByText('Nothing scanned yet this session.')).toBeVisible()

  await page.getByRole('button', { name: 'Scan network' }).click()
  await expect(page.getByText('calico', { exact: false }).first()).toBeVisible({ timeout: 10000 })
  await expect(page.getByText('billing-api', { exact: false }).first()).toBeVisible()
  expect(errors).toEqual([])
})

test('Cost & Waste shows the cheap report by default and can turn on usage', async ({ page }) => {
  const errors = await gotoPanel(page, 'costwaste', { demo: true })
  await expect(page.getByText('needs usage check', { exact: false })).toBeVisible()
  await expect(page.locator('table')).toContainText('worker')

  await page.getByRole('button', { name: 'Check actual usage' }).click()
  await expect(page.getByRole('button', { name: 'Actual usage on' })).toBeVisible()
  expect(errors).toEqual([])
})

test('Cost & Waste reports cluster-unreachable in live mode against the sandbox', async ({ page }) => {
  const errors = await gotoPanel(page, 'costwaste', { demo: false })
  await expect(page.getByText('cost couldn\'t be estimated', { exact: false })).toBeVisible()
  expect(errors).toEqual([])
})
