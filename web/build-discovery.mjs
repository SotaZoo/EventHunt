import { build } from 'esbuild'
import { readFile, rm } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const directory = fileURLToPath(new URL('.', import.meta.url))
const output = fileURLToPath(new URL('./dist/discovery', import.meta.url))
const iconLicense = await readFile(new URL('./discovery/icons/LICENSE', import.meta.url), 'utf8')
const ionicLicense = await readFile(new URL('./node_modules/@ionic/core/LICENSE', import.meta.url), 'utf8')
// This directory contains only this build's generated assets. Remove obsolete
// hashed chunks so reruns and wheels do not accumulate previous builds.
await rm(output, { recursive: true, force: true })
await build({
  absWorkingDir: directory,
  entryPoints: { main: 'discovery/main.ts', account: 'account/main.ts' },
  bundle: true,
  splitting: true,
  format: 'esm',
  target: 'es2022',
  loader: { '.webp': 'file' },
  outdir: output,
  minify: true,
  footer: { js: `/*! Lucide and Feather\n${iconLicense.trim()}\n\nIonic Core\n${ionicLicense.trim()}\n*/` },
  logLevel: 'info',
})
