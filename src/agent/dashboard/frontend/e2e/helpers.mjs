// Shared setup for every spec: a fresh page with known localStorage state
// (theme, demo flag, setup checklist dismissed so it doesn't cover the
// page) applied before any app script runs, then a navigation to the
// panel under test. Console/page errors are collected so a test can assert
// none happened — this is the class of bug (a bad demo-fixture key, a
// layout div stretching past the viewport) that only shows up at runtime,
// never in a lint pass.
export async function preparePage(page, { demo = true, theme = 'dark' } = {}) {
  const errors = []
  page.on('pageerror', err => errors.push(String(err)))
  page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()) })
  await page.addInitScript(([d, t]) => {
    localStorage.setItem('atlas-demo', d ? '1' : '0')
    localStorage.setItem('atlas-theme', t)
    localStorage.setItem('atlas-setup-hidden', '1')
    localStorage.setItem('atlas-chat-open', '1')
  }, [demo, theme])
  return errors
}

export async function gotoPanel(page, hash, opts) {
  const errors = await preparePage(page, opts)
  await page.goto(`/#${hash}`)
  await page.waitForLoadState('networkidle')
  return errors
}

// id -> the exact <h1> text App.jsx's TITLES renders for that panel (the
// page's own content div doesn't reliably repeat its name as visible text —
// Settings and Activity don't, for instance — so the title bar is the one
// reliable place to assert against).
export const NAV_PAGES = [
  ['overview', 'Overview'], ['cluster', 'Cluster'], ['databases', 'Databases'],
  ['network', 'Network'], ['costwaste', 'Cost & Waste'],
  ['jenkins', 'Jenkins'], ['github', 'GitHub Actions'], ['deploys', 'Deploys'],
  ['aws', 'AWS'], ['domains', 'Domains & HTTPS'],
  ['automation', 'Automation'], ['commands', 'Command Runner'], ['system', 'System'],
  ['incidents', 'Incidents & SLOs'], ['approvals', 'Approvals'], ['activity', 'Activity'],
  ['settings', 'Settings'], ['about', 'About AtlasOS'],
]
