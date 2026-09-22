/**
 * A very small Chrome DevTools Protocol driver.
 *
 * Headless Chromium's `--screenshot` / `--dump-dom` shortcuts wait for the page
 * load to finish, which never happens here: the app holds an open server-sent
 * events connection for the lifetime of the session. Driving CDP directly lets
 * us wait on our own terms, click things, and read the console.
 *
 * Node 22 provides a global WebSocket, so this needs no dependencies.
 */

import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const CHROME_FLAGS = [
  '--headless=new',
  '--no-sandbox',
  '--disable-gpu',
  '--disable-dev-shm-usage',
  '--disable-extensions',
  '--disable-background-networking',
  '--no-first-run',
  '--window-size=1600,1000',
]

/**
 * Launch Chromium already pointed at `url`.
 *
 * `Page.navigate` is deliberately not used: it does not resolve while the page
 * has an open server-sent-events connection, and the app keeps one for the
 * whole session. Starting at the URL sidesteps that entirely — the caller then
 * polls the DOM for whatever it is waiting on.
 */
export async function launch({ binary = 'chromium', port = 9333, url = 'about:blank' } = {}) {
  const profile = mkdtempSync(join(tmpdir(), 'rw-cdp-'))
  const child = spawn(
    binary,
    [...CHROME_FLAGS, `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, url],
    { stdio: ['ignore', 'pipe', 'pipe'] },
  )
  const stderr = []
  child.stderr.on('data', (chunk) => stderr.push(String(chunk)))

  const target = await waitForTarget(port, { url })
  const session = await connect(target.webSocketDebuggerUrl)

  return {
    session,
    stderr,
    async close() {
      try {
        session.socket.close()
      } catch {}
      child.kill('SIGKILL')
      await new Promise((resolve) => setTimeout(resolve, 100))
      rmSync(profile, { recursive: true, force: true })
    },
  }
}

async function waitForTarget(port, { url, timeoutMs = 20000 } = {}) {
  const deadline = Date.now() + timeoutMs
  let lastError
  let lastTargets = []
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`)
      const targets = await response.json()
      lastTargets = targets
      const pages = targets.filter((t) => t.type === 'page' && t.webSocketDebuggerUrl)
      // Chromium opens an initial about:blank tab; attaching to that one gives a
      // context where the application does not exist and every probe looks like
      // a silent failure. Insist on the page actually showing the target URL.
      const wanted =
        url && url !== 'about:blank'
          ? pages.find((t) => t.url.startsWith(url) || t.url.startsWith(url.replace(/\/$/, '')))
          : pages[0]
      if (wanted) return wanted
    } catch (error) {
      lastError = error
    }
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
  throw new Error(
    `no Chromium page target for ${url}: ${lastError ?? ''} ` +
      `(saw: ${lastTargets.map((t) => `${t.type}:${t.url}`).join(', ') || 'none'})`,
  )
}

function connect(url) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url)
    let nextId = 1
    const pending = new Map()
    const listeners = new Map()

    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      if (message.id && pending.has(message.id)) {
        const { resolve: done, reject: fail } = pending.get(message.id)
        pending.delete(message.id)
        if (message.error) fail(new Error(`${message.error.message} (${JSON.stringify(message.error)})`))
        else done(message.result)
        return
      }
      const handlers = listeners.get(message.method)
      if (handlers) for (const handler of handlers) handler(message.params)
    })
    socket.addEventListener('error', reject)
    socket.addEventListener('open', () =>
      resolve({
        socket,
        send(method, params = {}) {
          const id = nextId++
          return new Promise((done, fail) => {
            pending.set(id, { resolve: done, reject: fail })
            socket.send(JSON.stringify({ id, method, params }))
          })
        },
        on(method, handler) {
          if (!listeners.has(method)) listeners.set(method, [])
          listeners.get(method).push(handler)
        },
      }),
    )
  })
}

/** Evaluate an expression in the page and return its JSON value. */
export async function evaluate(session, expression) {
  const result = await session.send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  })
  if (result.exceptionDetails) {
    throw new Error(
      `page threw: ${result.exceptionDetails.exception?.description ?? result.exceptionDetails.text}`,
    )
  }
  return result.result.value
}

/** Poll an expression until it is truthy. */
export async function waitFor(session, expression, { timeoutMs = 15000, label = expression } = {}) {
  const deadline = Date.now() + timeoutMs
  let last
  while (Date.now() < deadline) {
    last = await evaluate(session, expression)
    if (last) return last
    await new Promise((resolve) => setTimeout(resolve, 150))
  }
  throw new Error(`timed out waiting for: ${label} (last value: ${JSON.stringify(last)})`)
}

/** Click the element centre, optionally holding a modifier. */
export async function clickSelector(session, selector, { modifier = 0 } = {}) {
  const box = await evaluate(
    session,
    `(() => {
       const el = document.querySelector(${JSON.stringify(selector)})
       if (!el) return null
       const r = el.getBoundingClientRect()
       return { x: r.left + r.width / 2, y: r.top + r.height / 2 }
     })()`,
  )
  if (!box) throw new Error(`no element matched ${selector}`)
  const base = { x: box.x, y: box.y, button: 'left', clickCount: 1, modifiers: modifier }
  await session.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...base })
  await session.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...base })
}

export const MODIFIER_CTRL = 2
export const MODIFIER_META = 4
