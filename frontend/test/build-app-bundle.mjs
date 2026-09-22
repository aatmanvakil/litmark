/** Bundle the real application entry so a DOM test can execute it. */
import * as esbuild from 'esbuild'

import { urlSuffixPlugin } from './url-suffix-plugin.mjs'

await esbuild.build({
  entryPoints: ['src/main.tsx'],
  bundle: true,
  format: 'iife',
  platform: 'browser',
  jsx: 'automatic',
  jsxImportSource: 'preact',
  define: { 'process.env.NODE_ENV': '"production"' },
  loader: { '.css': 'empty', '.ttf': 'empty', '.woff': 'empty', '.woff2': 'empty' },
  plugins: [urlSuffixPlugin],
  outfile: 'test/bundle/app.mjs',
  logLevel: 'warning',
})
