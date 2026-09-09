import { AsyncLocalStorage } from 'node:async_hooks'
import { randomUUID } from 'node:crypto'
import { createConnection } from 'node:net'

const MAX_BYTES = 1024 * 1024
const OPERATIONS = new Set([
  'search_knowledge_base', 'load_memory', 'save_memory', 'update_memory',
  'delete_memory', 'memory_status', 'list_skill_spaces', 'list_skills',
  'search_skills', 'load_skill', 'read_skill_resource', 'execute_skills', 'read_skill_artifact',
])

export class ResourceBridgeError extends Error {
  constructor(code) { super(code); this.code = code }
}

function assertJson(value, depth = 0, parents = new Set()) {
  if (depth > 64) throw new ResourceBridgeError('RESOURCE_REQUEST_INVALID')
  if (value === null || typeof value === 'boolean') return
  if (typeof value === 'string') {
    if (!value.isWellFormed()) throw new ResourceBridgeError('RESOURCE_REQUEST_INVALID')
    return
  }
  if (typeof value === 'number' && Number.isFinite(value)) return
  if (typeof value !== 'object' || parents.has(value)) {
    throw new ResourceBridgeError('RESOURCE_REQUEST_INVALID')
  }
  if (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype) {
    throw new ResourceBridgeError('RESOURCE_REQUEST_INVALID')
  }
  parents.add(value)
  for (const [key, child] of Object.entries(value)) {
    assertJson(key, depth + 1, parents)
    assertJson(child, depth + 1, parents)
  }
  parents.delete(value)
}

export class ResourceBridge {
  #context = new AsyncLocalStorage()
  #pending = new Set()
  #closed = false
  #socketPath
  #tools = new Map()

  constructor(socketPath) {
    if (typeof socketPath !== 'string' || !socketPath.startsWith('/') || socketPath.includes('\0')) {
      throw new ResourceBridgeError('RESOURCE_SOCKET_INVALID')
    }
    this.#socketPath = socketPath
  }

  registerTool(name, operation) {
    if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(name) || !OPERATIONS.has(operation) || this.#tools.has(name)) {
      throw new ResourceBridgeError('RESOURCE_TOOL_INVALID')
    }
    this.#tools.set(name, operation)
    return () => this.#tools.delete(name)
  }

  operationForTool(name) { return this.#tools.get(name) }

  withInvocation(context, callback) {
    // Only the trusted executor/MCP adapter establishes this context. The
    // opaque handle still requires independent authorization by Python.
    if (!context || typeof context.handle !== 'string' ||
        !/^[A-Za-z0-9_-]{32,256}$/.test(context.handle) ||
        !Number.isSafeInteger(context.deadline) || context.deadline <= Date.now() ||
        (context.signal != null && !(context.signal instanceof AbortSignal))) {
      throw new ResourceBridgeError('RESOURCE_CONTEXT_REQUIRED')
    }
    return this.#context.run(Object.freeze({
      handle: context.handle, deadline: context.deadline, signal: context.signal,
    }), callback)
  }

  async call(operation, args = {}, { signal } = {}) {
    return this.#request(operation, args, { signal }, false)
  }

  async check(operation) {
    return this.#request(operation, {}, {}, true)
  }

  async #request(operation, args, { signal }, checkOnly) {
    if (this.#closed) throw new ResourceBridgeError('RESOURCE_BRIDGE_CLOSED')
    if (this.#pending.size >= 64) throw new ResourceBridgeError('RESOURCE_BUSY')
    const context = this.#context.getStore()
    if (!context) throw new ResourceBridgeError('RESOURCE_CONTEXT_REQUIRED')
    if (signal != null && !(signal instanceof AbortSignal)) {
      throw new ResourceBridgeError('RESOURCE_REQUEST_INVALID')
    }
    const signals = [context.signal, signal].filter(Boolean)
    const callSignal = signals.length ? AbortSignal.any(signals) : undefined
    if (!OPERATIONS.has(operation) || !args || Array.isArray(args) || typeof args !== 'object') {
      throw new ResourceBridgeError('RESOURCE_REQUEST_INVALID')
    }
    assertJson(args)
    const requestId = randomUUID()
    const deadline = Math.min(context.deadline, Date.now() + (operation === 'execute_skills' ? 900_000 : 60_000))
    if (deadline <= Date.now()) throw new ResourceBridgeError('RESOURCE_DEADLINE_EXCEEDED')
    const body = Buffer.from(JSON.stringify({
      v: 1, requestId, deadline, operation, handle: context.handle, arguments: args,
      ...(checkOnly ? { checkOnly: true } : {}),
    }), 'utf8')
    if (body.length > MAX_BYTES) throw new ResourceBridgeError('RESOURCE_REQUEST_INVALID')
    const prefix = Buffer.alloc(4)
    prefix.writeUInt32BE(body.length)
    return new Promise((resolve, reject) => {
      let socket
      let timer
      let settled = false
      let received = Buffer.alloc(0)
      let expected
      const finish = (error, result) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        callSignal?.removeEventListener('abort', abort)
        this.#pending.delete(abort)
        socket?.destroy()
        if (error) reject(error)
        else resolve(result)
      }
      const fail = code => finish(new ResourceBridgeError(code))
      const abort = () => fail('RESOURCE_CANCELLED')
      if (callSignal?.aborted) return abort()
      this.#pending.add(abort)
      callSignal?.addEventListener('abort', abort, { once: true })
      timer = setTimeout(() => fail('RESOURCE_DEADLINE_EXCEEDED'), Math.max(1, deadline - Date.now()))
      socket = createConnection({ path: this.#socketPath })
      socket.once('connect', () => socket.write(Buffer.concat([prefix, body])))
      socket.once('error', () => fail('RESOURCE_TRANSPORT_FAILED'))
      socket.once('close', () => { if (!settled) fail('RESOURCE_TRANSPORT_FAILED') })
      socket.on('data', chunk => {
        if (received.length + chunk.length > MAX_BYTES + 4) return fail('RESOURCE_RESPONSE_INVALID')
        received = Buffer.concat([received, chunk])
        if (expected === undefined && received.length >= 4) {
          expected = received.readUInt32BE(0)
          if (!expected || expected > MAX_BYTES) return fail('RESOURCE_RESPONSE_INVALID')
        }
        if (expected === undefined || received.length < expected + 4) return
        if (received.length !== expected + 4) return fail('RESOURCE_RESPONSE_INVALID')
        try {
          const response = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(received.subarray(4)))
          if (response?.v !== 1 || response.requestId !== requestId) return fail('RESOURCE_RESPONSE_INVALID')
          if (response.error) {
            const code = response.error.code
            return fail(typeof code === 'string' && /^RESOURCE_[A-Z_]{1,64}$/.test(code)
              ? code : 'RESOURCE_RESPONSE_INVALID')
          }
          if (!response.result || typeof response.result !== 'object' || Array.isArray(response.result)) {
            return fail('RESOURCE_RESPONSE_INVALID')
          }
          assertJson(response.result)
          finish(null, response.result)
        } catch { fail('RESOURCE_RESPONSE_INVALID') }
      })
    })
  }

  dispose() {
    this.#closed = true
    for (const abort of [...this.#pending]) abort()
    this.#context.disable()
  }
}
