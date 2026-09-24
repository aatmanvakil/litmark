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
      name: 'International Macro',
      parent_id: null,
      path: 'International Macro',
      documents: [],
      document_count: 0,
      created_at: '2026-09-23T00:00:00Z',
      updated_at: '2026-09-23T00:00:00Z',
    },
    {
      collection_id: 'col-002',
      kind: 'topic',
      name: 'Dominant Currency',
      parent_id: 'col-001',
      path: 'International Macro / Dominant Currency',
      documents: ['doc-001'],
      document_count: 1,
      created_at: '2026-09-23T00:00:00Z',
      updated_at: '2026-09-23T00:00:00Z',
    },
    {
      collection_id: 'col-003',
      kind: 'topic',
      name: 'Deeply Nested',
      parent_id: 'col-002',
      path: 'International Macro / Dominant Currency / Deeply Nested',
      documents: [],
      document_count: 0,
      created_at: '2026-09-23T00:00:00Z',
      updated_at: '2026-09-23T00:00:00Z',
    },
    {
      collection_id: 'col-004',
      kind: 'topic',
      name: 'Orphaned Topic',
      parent_id: 'col-missing',
      path: 'Orphaned Topic',
      documents: [],
      document_count: 0,
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
  [
    '/api/conversations/conv-1',
    {
      conversation_id: 'conv-1',
      title: 'Chat',
      backend_session_id: null,
      messages: [],
      runs: [],
      proposals: [
        {
          proposal_id: 'prop-1',
          status: 'pending',
          query: 'exchange rate',
          work: {
            title: 'Exchange Rate Disconnect',
            authors: 'Anna Müller and Bo Lindqvist',
            year: 2021,
            journal: 'JPE',
            doi: '10.1234/exchange.2021',
          },
          versions: [
            {
              version_id: 'v1',
              version_type: 'accepted_manuscript',
              url: 'https://eprints.lse.ac.uk/1/accepted.pdf',
              host: 'repository',
              license: null,
              retrievable: true,
              source: 'unpaywall',
              reason: null,
            },
            {
              version_id: 'v2',
              version_type: 'published',
              url: 'https://doi.org/10.1234/exchange.2021',
              host: 'publisher',
              license: null,
              retrievable: false,
              source: 'unpaywall',
              reason: 'paywalled',
            },
          ],
          canonical_filename: 'Müller, Lindqvist (2021) – Exchange Rate Disconnect.pdf',
          assign_collection_id: null,
          chosen_version_id: null,
          document_id: null,
          error: null,
          attempts: 0,
          after_message_id: null,
          created_at: '2026-09-23T00:00:00Z',
          expires_at: '2099-01-01T00:00:00Z',
        },
      ],
    },
  ],
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
  const listeners = new Map()
  class FakeEventSource {
    constructor() {
      this.onopen = null
      this.onerror = null
      this.onmessage = null
    }
    addEventListener(type, handler) {
      listeners.set(type, [...(listeners.get(type) ?? []), handler])
    }
    close() {}
  }
  window.EventSource = FakeEventSource
  // The harness cannot open a real stream, so expose a way to deliver one
  // frame; a new event type is otherwise untestable here.
  window.__emit = (type, payload) => {
    for (const handler of listeners.get(type) ?? []) {
      handler({ data: JSON.stringify({ type, ...payload }), lastEventId: '1' })
    }
  }
  if (!window.matchMedia) {
    window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
  }
  window.scrollTo = () => {}
  // Not stubbed by jsdom; the sidebar uses both for create/rename/delete.
  window.prompt = () => null
  window.confirm = () => false
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

test('the collection tree renders nesting, and orphans survive it', async () => {
  const { window } = buildDom()
  window.eval(readFileSync(join(here, 'bundle', 'app.mjs'), 'utf8'))
  await new Promise((resolve) => setTimeout(resolve, 300))
  const text = window.document.querySelector('#app').textContent

  // Every level is present, including one whose parent does not exist.
  assert.ok(text.includes('International Macro'), 'root missing')
  assert.ok(text.includes('Dominant Currency'), 'child missing')
  assert.ok(text.includes('Deeply Nested'), 'grandchild missing')
  assert.ok(text.includes('Orphaned Topic'), 'a dangling parent lost its collection')

  // A twisty exists for a parent and points at a real subtree element.
  const twisty = window.document.querySelector('.twisty[aria-controls]')
  assert.ok(twisty, 'no expandable node rendered')
  const controlled = twisty.getAttribute('aria-controls')
  assert.ok(
    window.document.getElementById(controlled),
    `aria-controls="${controlled}" points at nothing`,
  )

  // Nested buttons would be invalid HTML and jsdom will not flag it.
  assert.equal(
    window.document.querySelectorAll('button button').length,
    0,
    'a button is nested inside another button',
  )
})

test('the assignment popover lists full paths and filters', async () => {
  const { window } = buildDom()
  window.eval(readFileSync(join(here, 'bundle', 'app.mjs'), 'utf8'))
  await new Promise((resolve) => setTimeout(resolve, 300))

  const trigger = [...window.document.querySelectorAll('button')].find(
    (button) => button.textContent.trim() === 'Collections…',
  )
  assert.ok(trigger, 'no assignment trigger rendered')
  trigger.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  await new Promise((resolve) => setTimeout(resolve, 50))

  const popover = window.document.querySelector('.assign-popover')
  assert.ok(popover, 'the popover did not open')
  // A nested collection is addressed by its path, not its bare name.
  assert.ok(
    popover.textContent.includes('International Macro / Dominant Currency'),
    `expected a full path, got: ${popover.textContent}`,
  )
  assert.ok(popover.querySelector('input[type="search"]'), 'no filter input')
  assert.equal(
    popover.querySelectorAll('input[type="checkbox"]').length,
    4,
    'every collection should be assignable',
  )
})

test('a pending proposal renders one card with every version', async () => {
  const { window } = buildDom()
  window.eval(readFileSync(join(here, 'bundle', 'app.mjs'), 'utf8'))
  await new Promise((resolve) => setTimeout(resolve, 300))

  const card = window.document.querySelector('.proposal')
  assert.ok(card, 'no proposal card rendered')
  const text = card.textContent

  // The three facts a confirmation must state.
  assert.ok(text.includes('eprints.lse.ac.uk'), `host missing: ${text}`)
  assert.ok(text.includes('Accepted manuscript'), `version type missing: ${text}`)
  assert.ok(
    text.includes('Müller, Lindqvist (2021) – Exchange Rate Disconnect.pdf'),
    `canonical filename missing: ${text}`,
  )

  // One card for the paper, not one per version.
  assert.equal(window.document.querySelectorAll('.proposal').length, 1)

  // A retrievable version offers a download; a paywalled one does not.
  const buttons = [...card.querySelectorAll('button.proposal-confirm')]
  assert.equal(buttons.length, 1, 'a paywalled version should have no button')
  assert.ok(buttons[0].textContent.includes('Download'), buttons[0].textContent)
  assert.ok(text.includes('paywalled'), 'the paywalled version was hidden')

  // Future tense only: nothing has been downloaded yet.
  assert.ok(text.includes('Will be saved as'), 'the card implies it already saved')
})

test('a download_proposed frame is not swallowed by the event switch', async () => {
  const { window } = buildDom()
  let conversationFetches = 0
  const realFetch = window.fetch
  window.fetch = async (input) => {
    const url = typeof input === 'string' ? input : input.url
    if (url.includes('/api/conversations/conv-1')) conversationFetches += 1
    return realFetch(input)
  }
  window.eval(readFileSync(join(here, 'bundle', 'app.mjs'), 'utf8'))
  await new Promise((resolve) => setTimeout(resolve, 300))

  const before = conversationFetches
  window.__emit('download_proposed', { seq: 2, proposal_id: 'prop-1' })
  await new Promise((resolve) => setTimeout(resolve, 200))

  assert.ok(
    conversationFetches > before,
    'the event did not trigger a reload, so it was dropped',
  )
})
