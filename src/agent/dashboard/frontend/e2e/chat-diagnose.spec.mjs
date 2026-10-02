// The assistant panel's Diagnose tab: it must show the real top open
// incident (from the demo fixture here) with its actual recorded cause —
// never an invented one — and "Diagnose with AI" must hand that straight
// into a real chat turn.
import { test, expect } from '@playwright/test'
import { gotoPanel } from './helpers.mjs'

test('Diagnose tab shows the open incident and hands off to chat', async ({ page }) => {
  const errors = await gotoPanel(page, 'overview', { demo: true })
  const assistant = page.locator('[aria-label="AtlasOS Assistant"]')

  await assistant.getByRole('tab', { name: /Diagnose/ }).click()
  await expect(assistant.getByText('Incident detected', { exact: false })).toBeVisible()
  await expect(assistant.getByText('api pods crashlooping after db-credentials rotation')).toBeVisible()

  await assistant.getByRole('button', { name: 'Diagnose with AI' }).click()
  await expect(assistant.getByRole('tab', { name: /Chat/ })).toHaveAttribute('aria-selected', 'true')
  await expect(assistant.getByText(/Summarize incident INC-142/)).toBeVisible()
  expect(errors).toEqual([])
})

test('Diagnose tab shows an all-clear state when nothing is open', async ({ page }) => {
  await gotoPanel(page, 'overview', { demo: false })
  const assistant = page.locator('[aria-label="AtlasOS Assistant"]')
  await assistant.getByRole('tab', { name: /Diagnose/ }).click()
  // The isolated test backend starts with no incidents at all.
  await expect(assistant.getByText('No open incidents')).toBeVisible()
})
