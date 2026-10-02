// Starts one throwaway dashboard server for the whole e2e run, isolated
// from the developer's real AtlasOS data: every store the backend opens
// (incident.db, slo.db, notifications.db, …) uses a path relative to the
// process's cwd, so running the server with cwd = a fresh temp directory
// gives every test its own databases, created fresh and discarded after —
// the real project's data/ is never touched. No keychain, no login token,
// so every page loads straight in.
//
// Playwright convention: the function globalSetup returns is used as
// globalTeardown automatically, so server + temp dir cleanup live right
// here next to where they're created.
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { PORT, BASE_URL } from './const.mjs'

const __dirname = fileURLToPath(new URL('.', import.meta.url))
const FRONTEND_DIR = resolve(__dirname, '..')
const REPO_ROOT = resolve(FRONTEND_DIR, '../../../..')
const VENV_PYTHON = join(REPO_ROOT, '.venv', 'Scripts', 'python.exe')

function run(cmd, args, options) {
  return new Promise((res, rej) => {
    const p = spawn(cmd, args, { stdio: 'inherit', shell: false, ...options })
    p.on('exit', code => (code === 0 ? res() : rej(new Error(`${cmd} exited ${code}`))))
    p.on('error', rej)
  })
}

async function waitForServer(timeoutMs = 25000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`${BASE_URL}/api/auth/status`)
      if (r.ok) return
    } catch { /* not up yet */ }
    await new Promise(r => setTimeout(r, 300))
  }
  throw new Error(`Dashboard test server didn't answer at ${BASE_URL} within ${timeoutMs}ms`)
}

export default async function globalSetup() {
  // Build once, fresh, so tests exercise the current source rather than a
  // dist/ left over from a manual run.
  await run('node', ['build.mjs'], { cwd: FRONTEND_DIR })

  const tempDir = mkdtempSync(join(tmpdir(), 'atlasos-e2e-'))
  const server = spawn(VENV_PYTHON, [
    '-m', 'uvicorn', 'agent.dashboard.server:app',
    '--host', '127.0.0.1', '--port', String(PORT), '--log-level', 'warning',
  ], {
    cwd: tempDir,
    env: { ...process.env, ATLASOS_NO_KEYRING: '1', DASHBOARD_TOKEN: ' ' },
    stdio: 'ignore',
  })
  server.on('error', err => { throw err })

  try {
    await waitForServer()
  } catch (err) {
    server.kill()
    rmSync(tempDir, { recursive: true, force: true })
    throw err
  }

  return async function globalTeardown() {
    await new Promise(resolve => {
      server.once('exit', resolve)
      server.kill()
      setTimeout(resolve, 5000) // don't hang teardown if it won't die cleanly
    })
    // Windows doesn't always release the SQLite file handles the instant
    // the process exits — retry past the brief EPERM/EBUSY window rather
    // than leaving the temp dir behind. A leftover temp dir is harmless
    // (the OS reclaims it eventually), so a final failure here is logged,
    // not thrown — it must never fail an otherwise-green test run.
    for (let attempt = 1; attempt <= 10; attempt++) {
      try {
        rmSync(tempDir, { recursive: true, force: true })
        return
      } catch (err) {
        if (attempt === 10) { console.warn(`[e2e] couldn't remove ${tempDir}: ${err.message}`); return }
        await new Promise(r => setTimeout(r, 400))
      }
    }
  }
}
