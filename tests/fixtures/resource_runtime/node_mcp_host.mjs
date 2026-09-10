import { createInterface } from 'node:readline'
import { pathToFileURL } from 'node:url'
import { join } from 'node:path'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]()
const input = JSON.parse((await lines.next()).value)
const load = name => import(pathToFileURL(join(input.nodeModules, '@deepseek-ai', name, 'lib/index.js')).href)
const { Context } = await load('cordis')
const { default: SystemPrompt } = await load('dsh-system-prompt')
const { default: ToolRuntime } = await load('dsh-tools')
const { default: WebServer } = await load('dsh-host-webserver')
const { default: Credentials } = await load('dsh-credentials-local')
const Connection = await load('dsh-client-connection')
const ctx = new Context()
const fibers = []
const credentialDir = await mkdtemp(join(tmpdir(), 'ksadk-resource-fixture-'))
Object.assign(process.env, {
  KSADK_DSH_PROFILE: 'studio', KSADK_DSH_PROFILE_DIGEST: input.profileDigest,
  KSADK_DSH_VERSION: '0.1.1-rc.2', KSADK_DSH_CAPABILITY_READY_STDOUT: '1',
})
try {
  // The capability host now shares the official Core HTTP/auth services.
  // A tools-only Core leaves those dependencies pending forever.
  fibers.push(await ctx.plugin(WebServer, { host: '127.0.0.1', port: 0 }))
  fibers.push(await ctx.plugin(Credentials, { path: join(credentialDir, 'credentials.yaml') }))
  fibers.push(await ctx.plugin(Connection, {}))
  fibers.push(await ctx.plugin(SystemPrompt, {}))
  fibers.push(await ctx.plugin(ToolRuntime, { mode: 'native' }))
  fibers.push(await ctx.plugin(await import(input.bridgePlugin), { socketPath: input.socketPath }))
  fibers.push(await ctx.plugin(await import(input.knowledgePlugin)))
  if (input.extraPlugin) fibers.push(await ctx.plugin(await import(input.extraPlugin)))
  fibers.push(await ctx.plugin(await import(input.hostPlugin)))
  await lines.next()
} finally {
  for (const fiber of fibers.reverse()) await fiber.dispose()
  await rm(credentialDir, { recursive: true, force: true })
}
