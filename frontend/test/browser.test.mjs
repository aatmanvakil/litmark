/**
 * Real-browser checks against a running server.
 *
 * These cover the criteria nothing else can answer: Markdown live preview
 * (A2), and opening a citation to a highlighted PDF passage at more than one
 * zoom level (A6/A8).
 *
 * Firefox, not Chromium: headless Chromium cannot reach loopback HTTP in every
 * sandbox — it returns an empty document where curl succeeds — so it silently
 * verifies nothing. See test/marionette.mjs.
 *
 * Run against a project that has at least one note containing a `source:`
 * citation:
 *
 *   RW_BASE_URL=http://127.0.0.1:8765/ npm run test:browser
 */

import assert from 'node:assert/strict'
import { after, before, describe, test } from 'node:test'

import { click, launchFirefox, navigate, script, waitFor } from './marionette.mjs'

const BASE_URL = process.env.RW_BASE_URL
const describeOrSkip = BASE_URL ? describe : describe.skip

describeOrSkip('the application in Firefox', () => {
  let firefox
  let client

  before(async () => {
    firefox = await launchFirefox({ binary: process.env.RW_FIREFOX ?? 'firefox' })
    client = firefox.client
    await navigate(client, BASE_URL)
    // The page holds an SSE connection open, so wait on the UI, not on load.
    await waitFor(client, `!!document.querySelector('.sidebar')`, {
      timeoutMs: 30000,
      label: 'the sidebar to render',
    })
  })

  after(async () => {
    await firefox?.close()
  })

  test('renders the project shell', async () => {
    const project = await script(client, `return document.querySelector('.topbar h1')?.textContent`)
    assert.ok(project, 'the project name is missing')

    const entries = await script(
      client,
      `return [...document.querySelectorAll('.sidebar .entry strong')].map((n) => n.textContent)`,
    )
    assert.ok(entries.length > 0, 'the sidebar listed nothing')
  })

  test('the event stream connects, so live updates work', async () => {
    // "Reconnecting…" showing steadily means the SSE response never opened.
    await waitFor(client, `!document.querySelector('.badge.warn')`, {
      timeoutMs: 10000,
      label: 'the reconnecting badge to clear',
    })
  })

  test('A2: Markdown renders in place, and Source mode shows the text', async () => {
    await waitFor(client, `!!document.querySelector('.cm-content')`, {
      label: 'the editor to mount',
    })
    assert.ok(
      await script(client, `return !!document.querySelector('.cm-heading')`),
      'no heading decoration was applied',
    )

    // Move the cursor onto a body line, so the heading's own markers hide.
    await script(
      client,
      `const el = document.querySelector('.cm-content');
       const line = [...el.querySelectorAll('.cm-line')].find(
         (n) => !n.textContent.trim().startsWith('#') && n.textContent.trim().length > 20,
       );
       if (line) {
         const range = document.createRange();
         range.setStart(line.firstChild ?? line, 0);
         range.collapse(true);
         const sel = window.getSelection();
         sel.removeAllRanges();
         sel.addRange(range);
       }
       return true;`,
    )
    await new Promise((resolve) => setTimeout(resolve, 300))

    const heading = await script(
      client,
      `return document.querySelector('.cm-heading')?.textContent ?? ''`,
    )
    assert.ok(!heading.trimStart().startsWith('#'), `heading markers still visible: ${heading}`)

    await click(client, '.editor-actions .toggle:not(.open-source)')
    await new Promise((resolve) => setTimeout(resolve, 300))
    const source = await script(
      client,
      `return document.querySelector('.cm-content')?.textContent ?? ''`,
    )
    assert.ok(source.includes('#'), 'Source mode did not reveal the Markdown syntax')
    assert.ok(source.includes('(source:'), 'Source mode did not reveal the citation syntax')

    await click(client, '.editor-actions .toggle:not(.open-source)')
    await new Promise((resolve) => setTimeout(resolve, 300))
  })

  test('a plain click on a citation arms the visible Open source action', async () => {
    assert.ok(
      await script(client, `return !!document.querySelector('.cm-source-link')`),
      'the open note contains no citation to click',
    )

    // A rendered citation is an atomic range; a plain click must still put the
    // cursor inside it rather than leaving the action disabled.
    await click(client, '.cm-source-link')
    await waitFor(client, `!document.querySelector('[data-action="open-source"]')?.disabled`, {
      timeoutMs: 5000,
      label: 'the Open source action to become enabled',
    })
    const label = await script(
      client,
      `return document.querySelector('[data-action="open-source"]')?.textContent ?? ''`,
    )
    assert.match(label, /Open source · ref-/, `unexpected action label: ${label}`)
  })

  test('A6/A8: opening a citation highlights the passage in the PDF', async () => {
    await click(client, '[data-action="open-source"]')

    await waitFor(client, `!!document.querySelector('.pdf canvas')`, {
      timeoutMs: 30000,
      label: 'the PDF canvas to render',
    })
    const canvas = await script(
      client,
      `const c = document.querySelector('.pdf canvas');
       return c ? { width: c.width, height: c.height } : null`,
    )
    assert.ok(canvas.width > 100 && canvas.height > 100, `PDF not drawn: ${JSON.stringify(canvas)}`)

    const highlights = await waitFor(
      client,
      `(() => {
         const nodes = [...document.querySelectorAll('.pdf-highlight')]
         return nodes.length
           ? nodes.map((n) => ({
               left: parseFloat(n.style.left),
               top: parseFloat(n.style.top),
               width: parseFloat(n.style.width),
               height: parseFloat(n.style.height),
             }))
           : null
       })()`,
      { timeoutMs: 20000, label: 'a highlight overlay' },
    )
    for (const rect of highlights) {
      assert.ok(rect.width > 1 && rect.height > 1, `degenerate highlight: ${JSON.stringify(rect)}`)
      assert.ok(
        rect.left >= 0 && rect.top >= 0,
        `highlight outside the page: ${JSON.stringify(rect)}`,
      )
    }
  })

  test('A6: the highlight stays on the passage across zoom levels', async () => {
    const measure = `(() => {
      const c = document.querySelector('.pdf canvas')
      const h = document.querySelector('.pdf-highlight')
      if (!c || !h) return null
      return {
        pageWidth: parseFloat(c.style.width),
        pageHeight: parseFloat(c.style.height),
        left: parseFloat(h.style.left),
        top: parseFloat(h.style.top),
        width: parseFloat(h.style.width),
      }
    })()`

    const before = await script(client, `return ${measure}`)
    assert.ok(before, 'no highlight to measure')

    await click(client, '.pdf-controls button[title="Zoom in"]')
    await waitFor(
      client,
      `(() => {
         const c = document.querySelector('.pdf canvas')
         return c && parseFloat(c.style.width) > ${before.pageWidth + 1}
       })()`,
      { timeoutMs: 15000, label: 'the page to grow after zooming in' },
    )
    await new Promise((resolve) => setTimeout(resolve, 400))

    const after = await script(client, `return ${measure}`)
    assert.ok(after, 'the highlight disappeared after zooming')

    // Normalized geometry means the highlight's position relative to the page
    // is invariant under zoom. Drift here is a coordinate-conversion bug.
    const relative = (m) => ({
      left: m.left / m.pageWidth,
      top: m.top / m.pageHeight,
      width: m.width / m.pageWidth,
    })
    const a = relative(before)
    const b = relative(after)
    assert.ok(Math.abs(a.left - b.left) < 0.01, `x drifted: ${a.left} -> ${b.left}`)
    assert.ok(Math.abs(a.top - b.top) < 0.01, `y drifted: ${a.top} -> ${b.top}`)
    assert.ok(Math.abs(a.width - b.width) < 0.01, `width drifted: ${a.width} -> ${b.width}`)
  })
})
