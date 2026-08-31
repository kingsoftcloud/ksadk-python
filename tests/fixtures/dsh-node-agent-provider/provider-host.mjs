import { createInterface } from 'node:readline'
import { pathToFileURL } from 'node:url'

// The host injects the exact Cordis entry resolved from its pinned, published
// DSH toolchain. This remains reproducible without a DSH source checkout and
// cannot accidentally bind to an ambient global package.
const cordisPath = process.env.KSADK_DSH_CORDIS_MODULE
if (!cordisPath) throw new Error('KSADK_DSH_CORDIS_MODULE is required')
const { Context } = await import(pathToFileURL(cordisPath).href)
const providerPlugin = await import('./index.mjs')
const ctx = new Context()
const fiber = await ctx.plugin(providerPlugin)
const provider = ctx.get('ksadkAgentProvider')
if (!provider) throw new Error('Cordis provider service did not become active')
let disposed = false

async function disposeCordis() {
  if (disposed) return
  disposed = true
  await fiber.dispose()
  await ctx.fiber.dispose()
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, () => {
    void disposeCordis().finally(() => process.exit(0))
  })
}

const handlers = {
  handshake: params => provider.handshake(params.profile),
  describe: params => provider.describe(params.profile),
  preflight: params => provider.preflight(params),
  activate: params => provider.activate(params),
  inventory: params => provider.inventory(params),
  health: params => provider.health(params),
  execute: params => provider.execute(params),
  cancel: params => provider.cancel(params),
  drain: params => provider.drain(params),
  dispose: params => provider.dispose(params),
}

function respond(id, result) {
  process.stdout.write(`${JSON.stringify({ id, result })}\n`)
}

function reject(id, error) {
  process.stdout.write(`${JSON.stringify({ id, error: { code: 'provider_rejected', message: error.message } })}\n`)
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity })
for await (const line of input) {
  let request
  try {
    request = JSON.parse(line)
    const handler = handlers[request.method]
    if (!handler) throw new Error('method is unavailable')
    const result = await handler(request.params ?? {})
    if (request.method === 'dispose' && request.params?.scope === 'host') {
      await disposeCordis()
    }
    respond(request.id, result)
    if (request.method === 'dispose' && request.params?.scope === 'host') break
  } catch (error) {
    reject(request?.id ?? 'unknown', error)
  }
}
