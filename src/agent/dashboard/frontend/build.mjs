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

// Write index.html
writeFileSync('dist/index.html', `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AtlasOS</title>
  <link rel="stylesheet" href="/assets/index.css" />
</head>
<body>
  <div id="root"></div>
  <script type="module" src="/assets/index.js"></script>
</body>
</html>
`)

console.log('\n✓ AtlasOS dashboard built → dist/')
