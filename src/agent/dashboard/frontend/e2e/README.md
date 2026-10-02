# Dashboard e2e tests

Real-browser tests (Playwright, against the system Edge — no Chromium
download needed) for the parts of the dashboard that a backend pytest run
can't see: layout, navigation, drag interactions, and whether a page throws
at runtime. This suite exists because the kind of bugs this project kept
shipping — a demo-fixture key typo, a panel silently stretching past the
viewport — only show up in a real rendered page, never in a lint pass.

## Running

```bash
npm run test:e2e        # headless, once
npm run test:e2e:ui     # Playwright's interactive UI mode
```

`global-setup.mjs` builds the frontend, starts one throwaway dashboard
server (port 8597) with its working directory in a fresh OS temp folder, and
tears it down after. Every SQLite store the backend opens (`incident.db`,
`slo.db`, `notifications.db`, …) is a path relative to that cwd, so the real
project's `data/` is never read or written — it's safe to run anytime,
including the mutating tests (`slo-crud.spec.mjs`).

No login, no OS keychain (`DASHBOARD_TOKEN=' '`, `ATLASOS_NO_KEYRING=1`), so
every page loads straight in.

## What's covered

- **smoke.spec.mjs** — every page, in both demo and live data, must render
  its title and throw no console/page error. Live mode against this
  unconfigured sandbox exercises every "couldn't check" path (no cluster,
  no Jenkins, no AWS credentials) for free.
- **resize-panel.spec.mjs** — dragging the assistant panel's handle actually
  changes its width, the width survives a reload, double-click resets it.
- **sidebar-nav.spec.mjs** — a nav group collapses/expands and the state
  survives a reload; a nested item navigates and updates the title.
- **chat-diagnose.spec.mjs** — the assistant's Diagnose tab shows the real
  top open incident (or an all-clear state), and hands off to a real chat
  turn.
- **slo-crud.spec.mjs** — create and delete an SLO end to end, the feature
  that used to be a dead "create one with `agent slo create`" message.

## Why the viewport is pinned

The assistant docks as a third column only at >=1340px; most of this suite
exercises that docked state. `playwright.config.mjs` sets a fixed
1600×1000 viewport deliberately and does **not** use a `devices[...]`
preset — those carry their own narrower default viewport that silently
overrides this one. That exact mistake made every docked-panel test fail
the first time this suite ran, against an app that had no bug at all.
