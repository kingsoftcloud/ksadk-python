const chunks = []
for await (const chunk of process.stdin) chunks.push(chunk)
const input = JSON.parse(Buffer.concat(chunks).toString('utf8'))
const { ResourceBridge } = await import(input.module)
const bridge = new ResourceBridge(input.socketPath)
try {
  const results = await Promise.all(input.calls.map(call => bridge.withInvocation({
    handle: call.handle, deadline: Date.now() + 20_000,
  }, async () => {
    await new Promise(resolve => setImmediate(resolve))
    return bridge.call('search_knowledge_base', { query: call.query })
  })))
  let missingContext
  try { await bridge.call('search_knowledge_base', { query: 'forbidden' }) }
  catch (error) { missingContext = error.code }
  process.stdout.write(JSON.stringify({ results, missingContext }))
} finally {
  bridge.dispose()
}
