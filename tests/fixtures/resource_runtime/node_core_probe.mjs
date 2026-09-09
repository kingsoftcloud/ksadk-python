import { pathToFileURL } from 'node:url'
import { join } from 'node:path'
const chunks = []
for await (const chunk of process.stdin) chunks.push(chunk)
const input = JSON.parse(Buffer.concat(chunks).toString('utf8'))
const load = name => import(pathToFileURL(join(input.nodeModules, '@deepseek-ai', name, 'lib/index.js')).href)
const { Context } = await load('cordis')
const { default: SystemPrompt } = await load('dsh-system-prompt')
const { default: ToolRuntime } = await load('dsh-tools')
const bridge = await import(input.bridgePlugin)
const knowledge = await import(input.businessPlugin ?? input.knowledgePlugin)
const toolName = input.toolName ?? 'search_knowledge_base'
const ctx = new Context()
const fibers = []
try {
  fibers.push(await ctx.plugin(SystemPrompt, {}))
  fibers.push(await ctx.plugin(ToolRuntime, { mode: 'native' }))
  fibers.push(await ctx.plugin(bridge, { socketPath: input.socketPath }))
  fibers.push(await ctx.plugin(knowledge))
  const names = ctx.tools.schemas().map(tool => tool.name)
  const executeCall = (call, index) =>
    ctx.platformResources.withInvocation({ handle: call.handle, deadline: Date.now() + 20_000 },
      () => ctx.tools.execute({ callId: `core-${index}`, name: call.name ?? toolName,
        arguments: call.arguments ?? { query: call.query }, signal: new AbortController().signal }))
  const results = []
  if (input.sequentialCalls) {
    for (const [index, call] of input.calls.entries()) results.push(await executeCall(call, index))
  } else {
    results.push(...await Promise.all(input.calls.map(executeCall)))
  }
  const missingContext = await ctx.tools.execute({ callId: 'no-context',
    name: toolName, arguments: input.missingContextArguments ?? { query: 'forbidden' },
    signal: new AbortController().signal })
  const memoryStatus = input.checkMemoryStatus
    ? await ctx.platformResources.withInvocation({ handle: input.calls[0].handle, deadline: Date.now() + 20_000 },
      () => ctx.tools.execute({ callId: 'memory-status', name: 'memory_status',
        arguments: { operationId: results[0].value.operationId }, signal: new AbortController().signal }))
    : null
  const skillArtifact = input.checkSkillArtifact
    ? await ctx.platformResources.withInvocation({ handle: input.calls[0].handle, deadline: Date.now() + 20_000 },
      () => ctx.tools.execute({ callId: 'skill-artifact', name: 'read_skill_artifact',
        arguments: { operationId: results[0].value.operationId, artifactId: results[0].value.artifacts[0].artifactId },
        signal: new AbortController().signal }))
    : null
  process.stdout.write(JSON.stringify({ names, results, missingContext, memoryStatus, skillArtifact }))
} finally {
  for (const fiber of fibers.reverse()) await fiber.dispose()
}
