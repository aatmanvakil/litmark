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
 *   LITMARK_BASE_URL=http://127.0.0.1:8765/ npm run test:browser
 */

import assert from 'node:assert/strict'
import { after, before, describe, test } from 'node:test'

import { click, launchFirefox, navigate, script, waitFor } from './marionette.mjs'

const BASE_URL = process.env.LITMARK_BASE_URL
const describeOrSkip = BASE_URL ? describe : describe.skip

describeOrSkip('the application in Firefox', () => {
  let firefox
  let client

  before(async () => {
    firefox = await launchFirefox({ binary: process.env.LITMARK_FIREFOX ?? 'firefox' })
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
      `return (() => {
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

  test('the document scrolls continuously, rendering pages lazily', async () => {
    const layout = await script(
      client,
      `return (() => {
         const scroll = document.querySelector('.pdf-scroll')
         const pages = [...document.querySelectorAll('.pdf-page')]
         return {
           pages: pages.length,
           drawn: pages.filter((p) => {
             const c = p.querySelector('canvas')
             return c && c.width > 0
           }).length,
           scrollHeight: scroll.scrollHeight,
           clientHeight: scroll.clientHeight,
           numbered: pages.map((p) => Number(p.dataset.page)),
         }
       })()`,
    )
    assert.ok(layout.pages > 1, 'only one page was laid out; the view is not continuous')
    assert.deepEqual(
      layout.numbered,
      Array.from({ length: layout.pages }, (_unused, index) => index + 1),
      'pages are not laid out in order',
    )
    assert.ok(
      layout.scrollHeight > layout.clientHeight,
      'the page stack does not exceed the viewport, so nothing can scroll',
    )
    assert.ok(layout.drawn >= 1, 'no page was rasterised')
  })

  test('scrolling updates the page indicator', async () => {
    const target = await script(
      client,
      `return (() => {
         const page = [...document.querySelectorAll('.pdf-page')].find(
           (n) => n.dataset.page === '2',
         )
         if (!page) return null
         page.scrollIntoView({ block: 'start' })
         return 2
       })()`,
    )
    if (target === null) return // single-page fixture

    // The indicator is measured from element rectangles, not the observer's
    // ratio, which a generous rootMargin would inflate to 1 for every page.
    await waitFor(client, `document.querySelector('.pdf-controls input')?.value === '2'`, {
      timeoutMs: 8000,
      label: 'the page indicator to follow the scroll',
    })
  })

  test('every mark in the document is shown, coloured by where it is cited', async () => {
    const tones = await script(
      client,
      `return (() => {
         const out = {}
         for (const node of document.querySelectorAll('.pdf-highlight')) {
           const tone = node.dataset.tone
           out[tone] = (out[tone] ?? 0) + 1
         }
         return out
       })()`,
    )
    const total = Object.values(tones).reduce((sum, n) => sum + n, 0)
    assert.ok(total > 1, `expected marks beyond the active one, saw ${JSON.stringify(tones)}`)

    // Marks from more than one origin must be distinguishable.
    const distinct = Object.keys(tones).filter((tone) => tones[tone] > 0)
    assert.ok(distinct.length >= 2, `only one kind of mark is drawn: ${distinct.join(', ')}`)

    const legend = await script(
      client,
      `return [...document.querySelectorAll('.pdf-legend .key')].map((n) => n.textContent.trim())`,
    )
    assert.ok(legend.length >= 2, `the key does not explain the colours: ${legend.join(' | ')}`)

    // The colours must actually differ, not merely carry different classes.
    const colours = await script(
      client,
      `return (() => {
         const seen = {}
         for (const node of document.querySelectorAll('.pdf-highlight')) {
           seen[node.dataset.tone] = getComputedStyle(node).backgroundColor
         }
         return seen
       })()`,
    )
    const values = Object.values(colours)
    assert.equal(
      new Set(values).size,
      values.length,
      `two mark kinds render identically: ${JSON.stringify(colours)}`,
    )
  })

  test('a mark carries a tooltip naming where it is cited', async () => {
    const titles = await script(
      client,
      `return [...document.querySelectorAll('.pdf-highlight')].map((n) => n.title).filter(Boolean)`,
    )
    assert.ok(titles.length > 0, 'marks carry no tooltip')
    assert.ok(
      titles.some((title) => /ref-/.test(title)),
      `tooltips do not name the reference: ${titles[0]}`,
    )
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
      `return (() => {
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

  test('A13: the sidebar writes a bibliography and reports what it found', async () => {
    const actions = await script(
      client,
      `return [...document.querySelectorAll('.sidebar .head-actions *')].map((n) =>
         n.textContent.trim(),
       )`,
    )
    assert.ok(actions.includes('Write .bib'), `no bibliography action: ${actions.join(' | ')}`)

    await click(client, '.sidebar .head-actions button')
    const banner = await waitFor(
      client,
      `return document.querySelector('.banner span')?.textContent ?? null`,
      { timeoutMs: 10000, label: 'the bibliography result banner' },
    )
    assert.match(banner, /references\.bib/, `unexpected banner: ${banner}`)
    assert.match(banner, /entr(y|ies)/, `the banner does not report a count: ${banner}`)

    // The download link must carry the session credential, or the browser
    // fetches it without the header the API requires and gets a 401.
    const href = await script(
      client,
      `return document.querySelector('.sidebar .head-actions a')?.getAttribute('href') ?? ''`,
    )
    assert.match(href, /format=bibtex/, `unexpected download link: ${href}`)
    assert.match(href, /token=./, `the download link carries no credential: ${href}`)
  })
})
