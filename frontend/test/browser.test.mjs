/**
 * Real-browser checks against a running server.
 *
 * These cover the criteria that only a browser can answer: Markdown live
 * preview (A2), and opening a citation to a highlighted PDF passage (A6/A8).
 *
 * Set RW_BASE_URL to point at a `research-workspace serve` instance that has at
 * least one note containing a `source:` citation. Skipped when it is unset.
 */

import assert from 'node:assert/strict'
import { after, before, describe, test } from 'node:test'

import { clickSelector, evaluate, launch, MODIFIER_CTRL, waitFor } from './cdp.mjs'

const BASE_URL = process.env.RW_BASE_URL
const describeOrSkip = BASE_URL ? describe : describe.skip

describeOrSkip('the application in Chromium', () => {
  let browser
  let session
  const consoleErrors = []

  before(async () => {
    browser = await launch({ binary: process.env.RW_CHROMIUM ?? 'chromium' })
    session = browser.session
    await session.send('Page.enable')
    await session.send('Runtime.enable')
    await session.send('Log.enable')
    session.on('Runtime.exceptionThrown', (params) => {
      consoleErrors.push(params.exceptionDetails?.exception?.description ?? 'exception')
    })
    session.on('Log.entryAdded', (params) => {
      if (params.entry.level === 'error') consoleErrors.push(params.entry.text)
    })
    await session.send('Page.navigate', { url: BASE_URL })
    // The app holds an SSE connection open, so wait on the UI, not on load.
    await waitFor(session, `!!document.querySelector('.sidebar')`, {
      label: 'the sidebar to render',
    })
  })

  after(async () => {
    await browser?.close()
  })

  test('renders the shell without console errors', async () => {
    const title = await evaluate(session, `document.querySelector('.topbar h1')?.textContent`)
    assert.ok(title && title.length > 0, 'the project name is missing')

    const documents = await evaluate(
      session,
      `[...document.querySelectorAll('.sidebar .entry strong')].map((n) => n.textContent)`,
    )
    assert.ok(documents.length > 0, 'no documents listed in the sidebar')

    const ignorable = /favicon|sourcemap|Download the React/i
    const real = consoleErrors.filter((message) => !ignorable.test(message))
    assert.deepEqual(real, [], `console errors: ${real.join('\n')}`)
  })

  test('A2: Markdown renders in place and the source toggle shows the text', async () => {
    // Open a note that has a heading and emphasis.
    await waitFor(session, `!!document.querySelector('.cm-content')`, {
      label: 'the editor to mount',
    })

    const hasHeading = await evaluate(
      session,
      `!!document.querySelector('.cm-heading')`,
    )
    assert.ok(hasHeading, 'no heading decoration was applied')

    // The hashes of a heading are hidden while the cursor is elsewhere.
    const headingText = await evaluate(
      session,
      `document.querySelector('.cm-heading')?.textContent ?? ''`,
    )
    assert.ok(!headingText.startsWith('#'), `heading markers are visible: ${headingText}`)

    // Source mode reveals the complete Markdown text.
    await clickSelector(session, '.editor-actions .toggle')
    await new Promise((resolve) => setTimeout(resolve, 250))
    const sourceText = await evaluate(
      session,
      `document.querySelector('.cm-content')?.textContent ?? ''`,
    )
    assert.ok(sourceText.includes('#'), 'source mode did not reveal the Markdown syntax')
    await clickSelector(session, '.editor-actions .toggle')
    await new Promise((resolve) => setTimeout(resolve, 250))
  })

  test('A6/A8: opening a citation shows the PDF with a highlight', async () => {
    const hasCitation = await evaluate(session, `!!document.querySelector('.cm-source-link')`)
    assert.ok(hasCitation, 'the open note contains no source citation to click')

    // A plain click must not navigate; it just places the cursor.
    await clickSelector(session, '.cm-source-link')
    await new Promise((resolve) => setTimeout(resolve, 400))

    // The documented affordance: a visible action, and Cmd/Ctrl-click.
    const openAction = await evaluate(
      session,
      `!!document.querySelector('[data-action="open-source"]:not([disabled])')`,
    )
    assert.ok(openAction, 'no visible "Open source" action for users who do not use the shortcut')

    await clickSelector(session, '[data-action="open-source"]')

    await waitFor(session, `!!document.querySelector('.pdf canvas')`, {
      timeoutMs: 25000,
      label: 'the PDF canvas to render',
    })

    const canvas = await evaluate(
      session,
      `(() => {
         const c = document.querySelector('.pdf canvas')
         return c ? { width: c.width, height: c.height } : null
       })()`,
    )
    assert.ok(canvas && canvas.width > 100 && canvas.height > 100, `PDF canvas not drawn: ${JSON.stringify(canvas)}`)

    const highlights = await waitFor(
      session,
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
      { timeoutMs: 15000, label: 'a highlight overlay' },
    )
    assert.ok(highlights.length > 0, 'the cited passage was not highlighted')
    for (const rect of highlights) {
      assert.ok(rect.width > 1 && rect.height > 1, `degenerate highlight: ${JSON.stringify(rect)}`)
      assert.ok(
        rect.left >= 0 && rect.top >= 0 && rect.left < canvas.width && rect.top < canvas.height,
        `highlight outside the page: ${JSON.stringify(rect)}`,
      )
    }
  })

  test('A6: the highlight tracks the page at a different zoom level', async () => {
    const before = await evaluate(
      session,
      `(() => {
         const c = document.querySelector('.pdf canvas')
         const h = document.querySelector('.pdf-highlight')
         if (!c || !h) return null
         return {
           pageWidth: parseFloat(c.style.width),
           left: parseFloat(h.style.left),
           width: parseFloat(h.style.width),
         }
       })()`,
    )
    assert.ok(before, 'no highlight to measure')

    // Zoom in one step.
    await clickSelector(session, '.pdf-controls button[title="Zoom in"]')
    await new Promise((resolve) => setTimeout(resolve, 1200))

    const after = await evaluate(
      session,
      `(() => {
         const c = document.querySelector('.pdf canvas')
         const h = document.querySelector('.pdf-highlight')
         if (!c || !h) return null
         return {
           pageWidth: parseFloat(c.style.width),
           left: parseFloat(h.style.left),
           width: parseFloat(h.style.width),
         }
       })()`,
    )
    assert.ok(after, 'the highlight disappeared after zooming')
    assert.ok(after.pageWidth > before.pageWidth, 'the page did not grow when zooming in')

    // The highlight must scale with the page: its relative position is fixed.
    const relBefore = before.left / before.pageWidth
    const relAfter = after.left / after.pageWidth
    assert.ok(
      Math.abs(relBefore - relAfter) < 0.01,
      `highlight drifted on zoom: ${relBefore.toFixed(4)} -> ${relAfter.toFixed(4)}`,
    )
    const widthBefore = before.width / before.pageWidth
    const widthAfter = after.width / after.pageWidth
    assert.ok(
      Math.abs(widthBefore - widthAfter) < 0.01,
      `highlight width drifted on zoom: ${widthBefore.toFixed(4)} -> ${widthAfter.toFixed(4)}`,
    )
  })
})
