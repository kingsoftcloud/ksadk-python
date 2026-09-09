import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { ResourceBridge } from '../../../ksadk/plugins/providers/bundles/dsh-platform-resources/client.mjs'

const context = () => ({ handle: 'h'.repeat(43), deadline: Date.now() + 5000 })

async function serverRun(handler, run) {
  const directory = await mkdtemp(join(tmpdir(), 'ksrn-'))
  const path = join(directory, 's')
  const sockets = new Set()
  const server = createServer(socket => {
    sockets.add(socket)
    socket.on('close', () => sockets.delete(socket))
    socket.on('error', () => {})
    let input = Buffer.alloc(0)
    socket.on('data', chunk => {
      input = Buffer.concat([input, chunk])
      if (input.length >= 4 && input.length === input.readUInt32BE(0) + 4) {
        handler(socket, JSON.parse(input.subarray(4).toString('utf8')))
      }
    })
  })
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(path, resolve) })
  const bridge = new ResourceBridge(path)
  try { await run(bridge) }
  finally {
    bridge.dispose()
    for (const socket of sockets) socket.destroy()
    await new Promise(resolve => server.close(resolve))
    await rm(directory, { recursive: true })
  }
}

function frame(value) {
  const body = Buffer.from(JSON.stringify(value))
  const header = Buffer.alloc(4)
  header.writeUInt32BE(body.length)
  return Buffer.concat([header, body])
}

test('fragmented replies are decoded and correlated', async () => {
  await serverRun((socket, request) => {
    const result = frame({ v: 1, requestId: request.requestId, result: { status: 'empty' } })
    socket.write(result.subarray(0, 2))
    setImmediate(() => socket.write(result.subarray(2)))
  }, bridge => bridge.withInvocation(context(), async () => {
    assert.deepEqual(await bridge.call('load_memory', { query: 'test' }), { status: 'empty' })
  }))
})

for (const kind of ['oversize', 'wrong-id', 'truncated', 'invalid-utf8', 'unsafe-error']) {
  test(`rejects ${kind} response without exposing upstream details`, async () => {
    await serverRun((socket, request) => {
      if (kind === 'oversize') {
        const header = Buffer.alloc(4); header.writeUInt32BE(1024 * 1024 + 1); socket.write(header)
      } else if (kind === 'truncated') {
        socket.end(Buffer.from([0, 0]))
      } else if (kind === 'invalid-utf8') {
        socket.write(Buffer.from([0, 0, 0, 1, 255]))
      } else if (kind === 'unsafe-error') {
        socket.write(frame({ v: 1, requestId: request.requestId, error: { code: 'fake-private-secret' } }))
      } else {
        socket.write(frame({ v: 1, requestId: 'another-request', result: {} }))
      }
    }, bridge => bridge.withInvocation(context(), async () => {
      await assert.rejects(bridge.call('load_memory', {}), error => {
        assert.match(error.code, /^RESOURCE_(RESPONSE_INVALID|TRANSPORT_FAILED)$/)
        assert.ok(!error.message.includes('fake-private-secret'))
        return true
      })
    }))
  })
}

test('rejects non-JSON and unknown operations before attempting transport', async () => {
  const bridge = new ResourceBridge('/nonexistent-resource-test-socket')
  try {
    await assert.rejects(bridge.call('load_memory', {}), { code: 'RESOURCE_CONTEXT_REQUIRED' })
    await bridge.withInvocation(context(), async () => {
      await assert.rejects(bridge.call('eval', {}), { code: 'RESOURCE_REQUEST_INVALID' })
      await assert.rejects(bridge.call('load_memory', { value: NaN }), { code: 'RESOURCE_REQUEST_INVALID' })
      await assert.rejects(bridge.call('load_memory', { value: '\ud800' }), { code: 'RESOURCE_REQUEST_INVALID' })
    })
  } finally { bridge.dispose() }
})

test('abort closes an in-flight socket', async () => {
  const controller = new AbortController()
  await serverRun(() => controller.abort(), bridge => bridge.withInvocation({
    ...context(), signal: controller.signal,
  }, async () => {
    await assert.rejects(bridge.call('load_memory', {}), { code: 'RESOURCE_CANCELLED' })
  }))
})
