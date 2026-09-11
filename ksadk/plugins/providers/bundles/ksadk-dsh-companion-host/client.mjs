import { AsyncLocalStorage } from 'node:async_hooks'
import { randomUUID } from 'node:crypto'
import { createConnection } from 'node:net'

const MAX_BYTES = 1024 * 1024
export class CompanionBridgeError extends Error {
  constructor(code) { super(code); this.code = code }
}
const fail = code => new CompanionBridgeError(code)

export class CompanionBridge {
  #configuration
  #context = new AsyncLocalStorage()
  #tools = new Map()
  #sockets = new Set()
  #closed = false

  constructor(config) {
    if (!config || typeof config.socketPath !== 'string' || !config.socketPath.startsWith('/') ||
        typeof config.secret !== 'string' || typeof config.generationId !== 'string') {
      throw fail('COMPANION_CONFIGURATION_INVALID')
    }
    this.#configuration = config
  }
  get generationId() { return this.#configuration.generationId }
  operationForTool(name) { return this.#tools.get(name) }

  async registerComponent(pluginId, component) {
    if (!this.#configuration.plugins?.[pluginId]?.components.includes(component)) {
      throw fail('COMPANION_COMPONENT_INVALID')
    }
    const socket = await this.#request({
      type: 'register', pluginId, component,
      generationId: this.generationId, secret: this.#configuration.secret,
    }, { keepOpen: true, deadline: Date.now() + 5000 })
    return () => { this.#sockets.delete(socket); socket.destroy() }
  }

  registerTool(name, pluginId, operation) {
    if (this.#tools.has(name) || !this.#configuration.plugins?.[pluginId]) {
      throw fail('COMPANION_TOOL_INVALID')
    }
    const entry = Object.freeze({ pluginId, operation })
    this.#tools.set(name, entry)
    return () => { if (this.#tools.get(name) === entry) this.#tools.delete(name) }
  }

  withInvocation(context, callback) {
    if (!context || context.generationId !== this.generationId ||
        !/^[A-Za-z0-9_-]{32,256}$/.test(context.handle) ||
        typeof context.callId !== 'string' || !context.callId || context.callId.length > 128 ||
        !Number.isSafeInteger(context.deadline) || context.deadline <= Date.now()) {
      throw fail('COMPANION_CONTEXT_REQUIRED')
    }
    return this.#context.run(Object.freeze({ ...context }), callback)
  }

  call(toolName, args, { signal } = {}) {
    const context = this.#context.getStore()
    const operation = this.#tools.get(toolName)
    if (!context || !operation || operation.pluginId !== context.pluginId) {
      throw fail('COMPANION_CONTEXT_REQUIRED')
    }
    const signals = [context.signal, signal].filter(Boolean)
    return this.#request({
      type: 'invoke', pluginId: operation.pluginId, operation: operation.operation,
      handle: context.handle, callId: context.callId, arguments: args,
      deadline: context.deadline,
    }, { deadline: context.deadline, signal: signals.length ? AbortSignal.any(signals) : undefined })
  }

  #request(request, { keepOpen = false, deadline, signal }) {
    if (this.#closed) throw fail('COMPANION_CLOSED')
    if (this.#sockets.size >= 96) throw fail('COMPANION_BUSY')
    const requestId = randomUUID()
    const body = Buffer.from(JSON.stringify({ v: 1, requestId, ...request }))
    if (body.length > MAX_BYTES) throw fail('COMPANION_REQUEST_INVALID')
    const prefix = Buffer.alloc(4)
    prefix.writeUInt32BE(body.length)
    return new Promise((resolve, reject) => {
      const socket = createConnection({ path: this.#configuration.socketPath })
      this.#sockets.add(socket)
      let received = Buffer.alloc(0), expected, settled = false
      const finish = (error, result) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        signal?.removeEventListener('abort', abort)
        if (!keepOpen || error) { this.#sockets.delete(socket); socket.destroy() }
        if (error) reject(error); else resolve(keepOpen ? socket : result)
      }
      const abort = () => finish(fail('COMPANION_CANCELLED'))
      const timer = setTimeout(() => finish(fail('COMPANION_OUTCOME_UNCERTAIN')), Math.max(1, deadline - Date.now()))
      if (signal?.aborted) { abort(); return }
      signal?.addEventListener('abort', abort, { once: true })
      socket.once('connect', () => socket.write(Buffer.concat([prefix, body])))
      socket.on('error', () => finish(fail('COMPANION_OUTCOME_UNCERTAIN')))
      socket.once('close', () => {
        this.#sockets.delete(socket)
        finish(fail('COMPANION_OUTCOME_UNCERTAIN'))
      })
      socket.on('data', chunk => {
        if (settled) return
        if (received.length + chunk.length > MAX_BYTES + 4) return finish(fail('COMPANION_RESPONSE_INVALID'))
        received = Buffer.concat([received, chunk])
        if (expected === undefined && received.length >= 4) {
          expected = received.readUInt32BE(0)
          if (!expected || expected > MAX_BYTES) return finish(fail('COMPANION_RESPONSE_INVALID'))
        }
        if (expected === undefined || received.length < expected + 4) return
        if (received.length !== expected + 4) return finish(fail('COMPANION_RESPONSE_INVALID'))
        try {
          const response = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(received.subarray(4)))
          if (response?.v !== 1 || response.requestId !== requestId) throw fail('COMPANION_RESPONSE_INVALID')
          if (response.error) throw fail(/^COMPANION_[A-Z_]+$/.test(response.error.code)
            ? response.error.code : 'COMPANION_RESPONSE_INVALID')
          if (!response.result || typeof response.result !== 'object' || Array.isArray(response.result)) {
            throw fail('COMPANION_RESPONSE_INVALID')
          }
          finish(null, response.result)
        } catch (error) { finish(error instanceof CompanionBridgeError ? error : fail('COMPANION_RESPONSE_INVALID')) }
      })
    })
  }

  dispose() {
    this.#closed = true
    for (const socket of this.#sockets) socket.destroy()
    this.#sockets.clear()
    this.#tools.clear()
    this.#context.disable()
  }
}
