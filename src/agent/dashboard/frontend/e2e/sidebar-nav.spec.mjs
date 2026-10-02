import { test, expect } from '@playwright/test'
import { gotoPanel } from './helpers.mjs'

test('a sidebar group collapses and expands, hiding and showing its pages', async ({ page }) => {
  await gotoPanel(page, 'overview')
  const group = page.getByRole('button', { name: 'Infrastructure' })
  const clusterLink = page.getByRole('button', { name: 'Cluster', exact: true })

  await expect(clusterLink).toBeVisible()
  await expect(group).toHaveAttribute('aria-expanded', 'true')

  await group.click()
  await expect(group).toHaveAttribute('aria-expanded', 'false')
  await expect(clusterLink).toBeHidden()

  await group.click()
  await expect(group).toHaveAttribute('aria-expanded', 'true')
  await expect(clusterLink).toBeVisible()
})

test('clicking a nested nav item navigates and updates the page title', async ({ page }) => {
  await gotoPanel(page, 'overview')
  await page.getByRole('button', { name: 'Databases', exact: true }).click()
  await expect(page).toHaveURL(/#databases$/)
  await expect(page.locator('h1')).toHaveText('Databases')
  await expect(page.locator('[data-screen-label]')).toBeVisible()
})

test('a group that stays collapsed survives a reload', async ({ page }) => {
  await gotoPanel(page, 'overview')
  await page.getByRole('button', { name: 'CI/CD', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Jenkins', exact: true })).toBeHidden()

  await page.reload()
  await page.waitForLoadState('networkidle')
  await expect(page.getByRole('button', { name: 'Jenkins', exact: true })).toBeHidden()
})
