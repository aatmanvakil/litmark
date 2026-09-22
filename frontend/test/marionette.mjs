/**
 * A minimal Marionette driver for headless Firefox.
 *
 * Chromium is the usual choice, but it cannot reach loopback HTTP in every
 * sandbox (it returns an empty document where curl succeeds), so Firefox is
 * the browser these tests can actually rely on. Marionette is Firefox's own
 * automation protocol: length-prefixed JSON over a TCP socket, no dependencies.
 */

import { spawn } from 'node:child_process'
import { connect } from 'node:net'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const PREFS = `
user_pref("marionette.port", %PORT%);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("app.update.enabled", false);
user_pref("extensions.autoDisableScopes", 15);
`

export async function launchFirefox({ binary = 'firefox', port = 2829, width = 1600, height = 1000 } = {}) {
  const profile = mkdtempSync(join(tmpdir(), 'rw-ff-'))
  writeFileSync(join(profile, 'user.js'), PREFS.replace('%PORT%', String(port)))

  const child = spawn(
    binary,
    ['--headless', '--marionette', '--profile', profile, '--window-size', `${width},${height}`, 'about:blank'],
    { stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, MOZ_HEADLESS: '1' } },
  )
  const stderr = []
  child.stderr.on('data', (chunk) => stderr.push(String(chunk)))

  const socket = await connectWithRetry(port)
  const client = makeClient(socket)
  await client.handshake()
  await client.command('WebDriver:NewSession', { capabilities: {} })

  return {
    client,
    stderr,
    async close() {
      try {
        await client.command('Marionette:Quit', {})
      } catch {}
      socket.destroy()
      child.kill('SIGKILL')
      await new Promise((resolve) => setTimeout(resolve, 200))
      rmSync(profile, { recursive: true, force: true })
    },
  }
}

async function connectWithRetry(port, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  let lastError
  while (Date.now() < deadline) {
    try {
      return await new Promise((resolve, reject) => {
        const socket = connect({ host: '127.0.0.1', port })
        socket.once('connect', () => resolve(socket))
        socket.once('error', reject)
      })
    } catch (error) {
      lastError = error
      await new Promise((resolve) => setTimeout(resolve, 300))
    }
  }
  throw new Error(`Marionette port ${port} never opened: ${lastError}`)
}

function makeClient(socket) {
  let buffer = Buffer.alloc(0)
  const queue = []
  let handshakeResolve = null

  socket.on('data', (chunk) => {
    buffer = Buffer.concat([buffer, chunk])
    // Framing is `<byte-length>:<json>`.
    for (;;) {
      const colon = buffer.indexOf(0x3a)
      if (colon === -1) return
      const length = Number.parseInt(buffer.subarray(0, colon).toString(), 10)
      if (!Number.isFinite(length)) return
      const start = colon + 1
      if (buffer.length < start + length) return
      const payload = JSON.parse(buffer.subarray(start, start + length).toString())
      buffer = buffer.subarray(start + length)

      if (handshakeResolve) {
        handshakeResolve(payload)
        handshakeResolve = null
        continue
      }
      const pending = queue.shift()
      if (!pending) continue
      // Responses are [type, id, error, result].
      const [, , error, result] = payload
      if (error) pending.reject(new Error(`${error.error}: ${error.message}`))
      else pending.resolve(result)
    }
  })

  let nextId = 1
  return {
    handshake() {
      return new Promise((resolve) => {
        handshakeResolve = resolve
      })
    },
    command(name, parameters = {}) {
      return new Promise((resolve, reject) => {
        queue.push({ resolve, reject })
        const message = JSON.stringify([0, nextId++, name, parameters])
        socket.write(`${Buffer.byteLength(message)}:${message}`)
      })
    },
  }
}

export async function navigate(client, url) {
  await client.command('WebDriver:Navigate', { url })
}

/** Run a script in the page and return its JSON value. */
export async function script(client, body, args = []) {
  const result = await client.command('WebDriver:ExecuteScript', {
    script: body,
    args,
    newSandbox: false,
  })
  return result?.value
}

export async function waitFor(client, expression, { timeoutMs = 20000, label = expression } = {}) {
  const deadline = Date.now() + timeoutMs
  let last
  // Marionette runs a script as a function body, so a bare expression yields
  // undefined. Accept either form rather than making every caller remember.
  const body = expression.trimStart().startsWith('return')
    ? expression
    : `return (${expression})`
  while (Date.now() < deadline) {
    last = await script(client, body)
    if (last) return last
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
  throw new Error(`timed out waiting for ${label} (last: ${JSON.stringify(last)})`)
}

/** Click an element, optionally with a modifier held. */
export async function click(client, selector, { ctrl = false } = {}) {
  const ok = await script(
    client,
    `const el = document.querySelector(arguments[0]);
     if (!el) return false;
     el.scrollIntoView({ block: 'center' });
     const opts = { bubbles: true, cancelable: true, ctrlKey: arguments[1], view: window };
     el.dispatchEvent(new MouseEvent('mousedown', opts));
     el.dispatchEvent(new MouseEvent('mouseup', opts));
     el.dispatchEvent(new MouseEvent('click', opts));
     return true;`,
    [selector, ctrl],
  )
  if (!ok) throw new Error(`no element matched ${selector}`)
}

export async function screenshot(client, path) {
  const result = await client.command('WebDriver:TakeScreenshot', { full: false })
  const data = result?.value ?? result
  if (typeof data === 'string') {
    const { writeFileSync } = await import('node:fs')
    writeFileSync(path, Buffer.from(data, 'base64'))
    return path
  }
  throw new Error('screenshot returned no data')
}
