/**
 * Executes the real application bundle in a DOM.
 *
 * This is the check that was missing: the server can serve every asset with a
 * 200 and the page can still be blank, because nothing had ever *run* the
 * JavaScript. Any import-time or first-render exception fails here.
 */

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'

const here = dirname(fileURLToPath(import.meta.url))
const STATE = {
  project: { name: 'Demo', root: '/tmp/demo', schema_version: 1 },
  agent: {
    backend: 'claude',
    installed: true,
    authenticated: true,
    ready: true,
    message: 'ready',
    detail: null,
  },
  documents: [
    {
      document_id: 'doc-001',
      sha256: 'abc',
      original_filename: 'paper.pdf',
      display_title: 'A Paper',
      title: 'A Paper',
      authors: null,
      year: null,
      page_count: 2,
      byte_size: 1000,
      imported_at: '2026-01-01T00:00:00Z',
      searchable: true,
      extraction: { status: 'ok', quality: 'ok', warnings: [], error: null },
      summary: { status: 'ready', error: null, generated_at: null },
      summary_text: '## Summary\n\nSomething [cited](source:ref-001).',
      summary_revision: 'rev-s',
    },
  ],
  notes: [
    { note_id: 'welcome', title: 'Welcome', revision: 'rev-1', modified_at: '2026-01-01T00:00:00Z' },
  ],
  collections: [
    {
      collection_id: 'col-001',
      kind: 'project',
      name: 'Minimum wage paper',
      documents: ['doc-001'],
      document_count: 1,
      created_at: '2026-09-23T00:00:00Z',
      updated_at: '2026-09-23T00:00:00Z',
    },
  ],
  unfiled: [],
  conversations: [{ conversation_id: 'conv-1', title: 'Chat', updated_at: '2026-01-01T00:00:00Z' }],
  latest_seq: 0,
}

const ROUTES = new Map([
  ['/api/state', STATE],
  ['/api/notes/welcome', { note_id: 'welcome', title: 'Welcome', revision: 'rev-1', modified_at: '2026-01-01T00:00:00Z', text: '# Welcome\n\nHello **world**.\n' }],
  ['/api/conversations/conv-1', { conversation_id: 'conv-1', title: 'Chat', backend_session_id: null, messages: [], runs: [] }],
  ['/api/changes', { changes: [] }],
])

function buildDom() {
  const html = readFileSync(join(here, '..', '..', 'src', 'litmark', 'static', 'index.html'), 'utf8')
    .replace('<meta name="litmark-token" content="">', '<meta name="litmark-token" content="test-token">')
    // The bundle is injected manually below, so strip the script tags.
    .replace(/<script[^>]*><\/script>/g, '')
    .replace(/<link[^>]*>/g, '')

  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8765/',
    pretendToBeVisual: true,
    runScripts: 'outside-only',
  })
  const { window } = dom

  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input.url
    const path = url.replace('http://127.0.0.1:8765', '').split('?')[0]
    const body = ROUTES.get(path)
    if (body === undefined) {
      return { ok: false, status: 404, text: async () => JSON.stringify({ error: { code: 'not_found', message: path } }) }
    }
    return { ok: true, status: 200, text: async () => JSON.stringify(body) }
  }
  class FakeEventSource {
    constructor() {
      this.onopen = null
      this.onerror = null
      this.onmessage = null
    }
    addEventListener() {}
    close() {}
  }
  window.EventSource = FakeEventSource
  if (!window.matchMedia) {
    window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
  }
  window.scrollTo = () => {}
  return dom
}

test('the built application bundle runs and renders without throwing', async () => {
  const dom = buildDom()
  const { window } = dom
  const errors = []
  window.addEventListener('error', (event) => errors.push(event.error ?? event.message))
  window.addEventListener('unhandledrejection', (event) => errors.push(event.reason))

  const bundle = readFileSync(join(here, 'bundle', 'app.mjs'), 'utf8')

  // Run the bundle exactly as the browser would.
  let thrown = null
  try {
    window.eval(bundle)
  } catch (error) {
    thrown = error
  }

  assert.equal(thrown, null, `the bundle threw on load: ${thrown?.stack ?? thrown}`)

  // Give the mounted component its first effects and resolved fetches.
  await new Promise((resolve) => setTimeout(resolve, 300))

  assert.deepEqual(errors, [], `runtime errors: ${errors.map((e) => e?.stack ?? e).join('\n')}`)

  const root = window.document.getElementById('app')
  assert.ok(root, 'the mount point is missing')
  assert.ok(root.innerHTML.length > 0, 'the application rendered nothing into #app')
  const text = root.textContent ?? ''
  assert.ok(!text.includes('Opening the project…'), `stuck on the loading state: ${text.slice(0, 200)}`)
  assert.ok(text.includes('Demo'), `project name not rendered: ${text.slice(0, 300)}`)
})
