// Lightweight esbuild bundler — replaces Vite production build.
// Uses ~10x less memory than Rolldown/Vite and runs on any Node version.
import { build } from 'esbuild'
import { mkdirSync, copyFileSync, writeFileSync } from 'fs'
import { resolve } from 'path'

const dist = resolve('dist')
const assets = resolve('dist/assets')
mkdirSync(assets, { recursive: true })

// Fonts (Inter, JetBrains Mono) and the Phosphor icon font are bundled from
// node_modules — no CDN at runtime. The server serves dist/assets at /assets.
const fontLoaders = { '.woff2': 'file', '.woff': 'file', '.ttf': 'file', '.eot': 'file', '.svg': 'file' }
const assetOpts = { assetNames: '[name]-[hash]', publicPath: '/assets' }

// Bundle JS + JSX
const result = await build({
  entryPoints: ['src/main.jsx'],
  bundle: true,
  outfile: 'dist/assets/index.js',
  format: 'esm',
  jsx: 'automatic',
  loader: { '.jsx': 'jsx', '.js': 'js', '.css': 'css', ...fontLoaders },
  ...assetOpts,
  minify: true,
  sourcemap: false,
  target: ['esnext'],
  define: {
    'process.env.NODE_ENV': '"production"',
  },
  logLevel: 'info',
}).catch(() => process.exit(1))

// Bundle CSS separately
await build({
  entryPoints: ['src/index.css'],
  bundle: true,
  outfile: 'dist/assets/index.css',
  loader: { '.css': 'css', ...fontLoaders },
  ...assetOpts,
  minify: true,
  logLevel: 'info',
}).catch(() => process.exit(1))

// Favicon, also used as the one PWA icon (an inline SVG works as an "any"-
// size icon in current Chromium/Edge — no raster generation needed).
copyFileSync('public/favicon.svg', 'dist/favicon.svg')

// Installable as a desktop/mobile app: a manifest plus a service worker.
// Chromium's install prompt wants both present, even a worker this trivial
// — it intentionally caches nothing (the dashboard changes too often for a
// stale-asset policy to be worth it) and just lets every request through.
writeFileSync('dist/manifest.webmanifest', JSON.stringify({
  name: 'AtlasOS',
  short_name: 'AtlasOS',
  description: 'Agentic DevOps OS — cluster, CI/CD, cost and incidents in one dashboard.',
  start_url: '/',
  scope: '/',
  display: 'standalone',
  background_color: '#161826',
  theme_color: '#161826',
  icons: [{ src: '/favicon.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'any' }],
}, null, 2))

writeFileSync('dist/sw.js', `// Intentionally a pass-through: present so the browser considers AtlasOS
// installable, caches nothing so there is no stale-asset policy to manage.
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()))
self.addEventListener('fetch', () => {})
`)

// Write index.html
writeFileSync('dist/index.html', `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AtlasOS</title>
  <meta name="theme-color" content="#161826" />
  <link rel="icon" id="favicon" type="image/svg+xml" href="/favicon.svg" />
  <link rel="manifest" href="/manifest.webmanifest" />
  <link rel="stylesheet" href="/assets/index.css" />
</head>
<body>
  <div id="root"></div>
  <script type="module" src="/assets/index.js"></script>
</body>
</html>
`)

console.log('\n✓ AtlasOS dashboard built → dist/')
