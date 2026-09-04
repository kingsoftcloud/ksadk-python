import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { spawn } from 'node:child_process'

const args = process.argv.slice(2)
if (args.includes('--dump-config')) {
  process.stdout.write(process.env.KSADK_DSH_FAKE_CONFIG ?? '[]\n')
  process.exit(0)
}

const startDelayMs = Number(process.env.KSADK_DSH_FAKE_START_DELAY_MS ?? 0)
if (Number.isFinite(startDelayMs) && startDelayMs > 0) {
  await new Promise((resolve) => setTimeout(resolve, startDelayMs))
}

const patchIndex = args.indexOf('--patch')
if (patchIndex < 0 || !args[patchIndex + 1]) throw new Error('missing capability overlay')
const patch = readFileSync(args[patchIndex + 1], 'utf8')
const match = patch.match(/name:\s*("[^\n]+")/)
if (!match) throw new Error('capability entrypoint is missing')
const plugin = await import(JSON.parse(match[1]))

const listeners = new Map()
const disposers = []
const defaultSchemas = [{
  name: 'fixture_echo',
  description: 'Echo',
  parameters: {
    type: 'object',
    properties: { message: { type: 'string' }, delayMs: { type: 'integer' } },
    required: ['message'],
    additionalProperties: false,
  },
}]
const schemas = process.env.KSADK_DSH_FAKE_SCHEMAS_JSON
  ? JSON.parse(process.env.KSADK_DSH_FAKE_SCHEMAS_JSON)
  : defaultSchemas

const tools = {
  schemas: () => structuredClone(schemas),
  async execute(exec) {
    if (exec.name !== 'fixture_echo') {
      return {
        isError: true,
        error: { message: 'unknown tool', info: { name: 'ToolNotFoundError', code: 'UNKNOWN_TOOL' } },
        content: [{ type: 'text', text: 'Error: unknown tool' }],
      }
    }
    const delayMs = exec.arguments.delayMs ?? 0
    if (exec.arguments.message === 'ignore-signal') {
      await new Promise(() => {})
    }
    if (delayMs > 0) {
      try {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(resolve, delayMs)
          const abort = () => {
            clearTimeout(timer)
            reject(exec.signal.reason ?? new Error('aborted'))
          }
          if (exec.signal.aborted) abort()
          else exec.signal.addEventListener('abort', abort, { once: true })
        })
      } catch {
        return {
          isError: true,
          error: { message: 'fixture call aborted', info: { name: 'AbortError', code: 'ABORTED' } },
          content: [{ type: 'text', text: 'Error: fixture call aborted' }],
        }
      }
    }
    const message = exec.arguments.message === 'large' ? 'x'.repeat(5000) : exec.arguments.message
    const value = { message, digest: createHash('sha256').update(message).digest('hex') }
    return {
      isError: false,
      value,
      content: [{ type: 'text', text: value.message }],
    }
  },
}

const ctx = {
  tools,
  on(event, listener) {
    listeners.set(event, listener)
    return () => listeners.delete(event)
  },
  effect(callback) {
    const disposer = callback()
    if (typeof disposer === 'function') disposers.push(disposer)
    return disposer
  },
}

await plugin.apply(ctx, {
  callTimeoutMs: 500,
  maxArgumentBytes: 1024,
  maxResultBytes: 4096,
  maxRequestBytes: 8192,
  maxInFlight: 8,
  inventoryQuietMs: 10,
})

const crashChildPidFile = process.env.KSADK_DSH_FAKE_CRASH_CHILD_PID_FILE
if (crashChildPidFile) {
  const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 60_000)'], {
    stdio: 'ignore',
  })
  writeFileSync(crashChildPidFile, String(child.pid), 'utf8')
  setTimeout(() => process.exit(17), 50)
}

async function shutdown() {
  for (const dispose of disposers.reverse()) await dispose()
  process.exit(0)
}
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)
