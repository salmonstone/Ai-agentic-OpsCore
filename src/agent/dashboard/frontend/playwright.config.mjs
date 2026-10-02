import { defineConfig } from '@playwright/test'
import { BASE_URL } from './e2e/const.mjs'

// Runs against the system Edge (channel 'msedge') instead of a downloaded
// Chromium, so `npx playwright install` isn't needed on a machine that
// already has Edge — which every target machine here does.
//
// The viewport is deliberately wide: at >=1340px the assistant docks as a
// third column instead of floating, which is what most of these tests
// exercise (the resize handle, the Diagnose tab). Don't add a device
// preset here — devices['Desktop Edge'] etc. carry their own narrower
// viewport that silently overrides this one, which is exactly the bug
// that made every docked-panel test fail the first time this suite ran.
export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.mjs',
  globalSetup: './e2e/global-setup.mjs',
  fullyParallel: true,
  // Capped rather than the default (half the CPU cores): this suite drives
  // real mouse drags and tab interactions, which got flaky timeouts under
  // a big unthrottled burst of concurrent browser contexts.
  workers: 3,
  timeout: 45000,
  retries: 1,
  reporter: 'list',
  use: {
    baseURL: BASE_URL,
    channel: 'msedge',
    viewport: { width: 1600, height: 1000 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
})
