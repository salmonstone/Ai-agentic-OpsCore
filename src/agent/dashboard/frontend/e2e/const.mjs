// Shared between playwright.config.mjs and the global setup/teardown.
// A fixed, high, unusual port so it never collides with the real dashboard
// (8501) or anything else a developer might have running.
export const PORT = 8597
export const BASE_URL = `http://127.0.0.1:${PORT}`
