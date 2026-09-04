import { createHash, createHmac, randomBytes, randomUUID, timingSafeEqual } from 'node:crypto'
import { createServer } from 'node:http'

export const name = 'ksadk-dsh-capability-host'
export const inject = ['tools']

export const HOST_PROTOCOL = 'ksadk.dsh-capability-host/v1'
export const HOST_VERSION = '1.0.0'
export const MCP_PROTOCOL_VERSION = '2025-06-18'
export const DEFAULT_CALL_TIMEOUT_MS = 60_000
export const DEFAULT_MAX_ARGUMENT_BYTES = 256 * 1024
export const DEFAULT_MAX_RESULT_BYTES = 1024 * 1024
export const DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
export const DEFAULT_MAX_IN_FLIGHT = 64

const SUPPORTED_MCP_PROTOCOLS = new Set([
  MCP_PROTOCOL_VERSION,
  '2025-03-26',
  '2024-11-05',
])
const DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/
const TOOL_NAME_PATTERN = /^[A-Za-z0-9_.:-]+$/

class RequestTooLargeError extends Error {}

function byteLength(value) {
  return Buffer.byteLength(JSON.stringify(value), 'utf8')
}

function assertUnicodeScalars(value) {
  if (typeof value === 'string') {
    for (let index = 0; index < value.length; index += 1) {
      const code = value.charCodeAt(index)
      if (code >= 0xD800 && code <= 0xDBFF) {
        const next = value.charCodeAt(index + 1)
        if (!(next >= 0xDC00 && next <= 0xDFFF)) {
          throw new Error('DSH tool schema contains invalid Unicode')
        }
        index += 1
      } else if (code >= 0xDC00 && code <= 0xDFFF) {
        throw new Error('DSH tool schema contains invalid Unicode')
      }
    }
    return
  }
  if (Array.isArray(value)) {
    for (const item of value) assertUnicodeScalars(item)
    return
  }
  if (value !== null && typeof value === 'object') {
    for (const [key, child] of Object.entries(value)) {
      assertUnicodeScalars(key)
      assertUnicodeScalars(child)
    }
  }
}

function canonical(value) {
  if (value === null) return Buffer.from('n;')
  if (typeof value === 'boolean') return Buffer.from(value ? 'b1;' : 'b0;')
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('tool schemas require finite JSON numbers')
    const bits = Buffer.allocUnsafe(8)
    bits.writeDoubleBE(Object.is(value, -0) ? 0 : value)
    return Buffer.from(`d${bits.toString('hex')};`)
  }
  if (typeof value === 'string') {
    const encoded = Buffer.from(value, 'utf8')
    return Buffer.concat([Buffer.from(`s${encoded.length}:`), encoded])
  }
  if (Array.isArray(value)) {
    return Buffer.concat([Buffer.from(`a${value.length}:`), ...value.map(canonical)])
  }
  if (value !== null && typeof value === 'object') {
    const keys = Object.keys(value).sort((left, right) =>
      Buffer.compare(Buffer.from(left, 'utf8'), Buffer.from(right, 'utf8')))
    return Buffer.concat([
      Buffer.from(`o${keys.length}:`),
      ...keys.flatMap((key) => [canonical(key), canonical(value[key])]),
    ])
  }
  throw new Error('tool schemas must contain only JSON values')
}

function digest(value) {
  return `sha256:${createHash('sha256').update(canonical(value)).digest('hex')}`
}

function positiveInteger(value, fallback, label, maximum) {
  const resolved = value === undefined ? fallback : value
  if (!Number.isSafeInteger(resolved) || resolved < 1 || resolved > maximum) {
    throw new Error(`${label} must be a positive bounded integer`)
  }
  return resolved
}

function requiredEnvironment(name, maximum) {
  const value = process.env[name]
  if (typeof value !== 'string' || value.length < 1 || value.length > maximum || value.includes('\0')) {
    throw new Error(`${name} is required and must be bounded`)
  }
  return value
}

function secureTokenEqual(actual, expected) {
  const left = Buffer.from(actual, 'utf8')
  const right = Buffer.from(expected, 'utf8')
  return left.length === right.length && timingSafeEqual(left, right)
}

function parseScopedToken(presented, token, profileDigest, tools, allowExpired = false) {
  const parts = presented.split('.')
  if (parts.length !== 3 || parts[0] !== 'ks1') return null
  const [, payloadEncoded, signature] = parts
  const expected = createHmac('sha256', token).update(payloadEncoded).digest('base64url')
  if (!secureTokenEqual(signature, expected)) return null
  let payload
  try {
    payload = JSON.parse(Buffer.from(payloadEncoded, 'base64url').toString('utf8'))
  } catch {
    return null
  }
  if (
    payload === null ||
    typeof payload !== 'object' ||
    Array.isArray(payload) ||
    payload.v !== 1 ||
    payload.profileDigest !== profileDigest ||
    typeof payload.jti !== 'string' ||
    !/^[A-Za-z0-9_-]{16,64}$/.test(payload.jti) ||
    !Number.isSafeInteger(payload.exp) ||
    (!allowExpired && payload.exp < Math.floor(Date.now() / 1000)) ||
    payload.exp > Math.floor(Date.now() / 1000) + 86_400 ||
    payload.aliases === null ||
    typeof payload.aliases !== 'object' ||
    Array.isArray(payload.aliases) ||
    Object.keys(payload.aliases).length < 1 ||
    Object.keys(payload.aliases).length > 32
  ) return null
  const aliases = new Map(Object.entries(payload.aliases))
  if ([...aliases].some(([alias, source]) =>
    !/^[A-Za-z0-9_-]{1,64}$/.test(alias) ||
    typeof source !== 'string' ||
    !tools.some((tool) => tool.name === source)
  )) return null
  return { root: false, scopeId: signature, aliases, expiresAt: payload.exp }
}

function authorize(request, token, profileDigest, tools, revokedScopes) {
  const header = request.headers.authorization
  if (typeof header !== 'string' || !header.startsWith('Bearer ')) return null
  if (secureTokenEqual(header, `Bearer ${token}`)) {
    return { root: true, scopeId: 'root', aliases: null, expiresAt: null }
  }
  const now = Math.floor(Date.now() / 1000)
  for (const [scopeId, expiresAt] of revokedScopes) {
    if (expiresAt < now) revokedScopes.delete(scopeId)
  }
  const authorization = parseScopedToken(
    header.slice('Bearer '.length), token, profileDigest, tools)
  if (authorization === null || revokedScopes.has(authorization.scopeId)) return null
  return authorization
}

function originAllowed(request) {
  const origin = request.headers.origin
  if (origin === undefined) return true
  if (Array.isArray(origin) || origin.length > 2048) return false
  try {
    const parsed = new URL(origin)
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') &&
      (parsed.hostname === 'localhost' || parsed.hostname === '127.0.0.1' || parsed.hostname === '[::1]')
  } catch {
    return false
  }
}

function writeJson(response, status, payload, extraHeaders = {}) {
  const body = Buffer.from(JSON.stringify(payload), 'utf8')
  response.writeHead(status, {
    'Cache-Control': 'no-store',
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': String(body.length),
    ...extraHeaders,
  })
  response.end(body)
}

function writeEmpty(response, status) {
  response.writeHead(status, {
    'Cache-Control': 'no-store',
    'Content-Length': '0',
  })
  response.end()
}

function jsonRpcError(id, code, message, data) {
  return {
    jsonrpc: '2.0',
    id: id ?? null,
    error: {
      code,
      message,
      ...(data === undefined ? {} : { data }),
    },
  }
}

function readBody(request, maximum) {
  return new Promise((resolve, reject) => {
    const declared = Number(request.headers['content-length'])
    if (Number.isFinite(declared) && declared > maximum) {
      request.resume()
      reject(new RequestTooLargeError('request body exceeds the configured limit'))
      return
    }
    const chunks = []
    let size = 0
    let settled = false
    request.on('data', (chunk) => {
      if (settled) return
      size += chunk.length
      if (size > maximum) {
        settled = true
        chunks.length = 0
        reject(new RequestTooLargeError('request body exceeds the configured limit'))
        return
      }
      chunks.push(chunk)
    })
    request.on('end', () => {
      if (!settled) resolve(Buffer.concat(chunks).toString('utf8'))
    })
    request.on('error', (error) => {
      if (!settled) reject(error)
    })
  })
}

function snapshotTools(runtime, maximumBytes) {
  const raw = runtime.schemas()
  if (!Array.isArray(raw) || raw.length > 2048) {
    throw new Error('DSH tool inventory is invalid or too large')
  }
  const seen = new Set()
  const tools = raw.map((schema) => {
    if (schema === null || typeof schema !== 'object' || Array.isArray(schema)) {
      throw new Error('DSH emitted an invalid tool schema')
    }
    const { name, description, parameters } = schema
    if (
      typeof name !== 'string' ||
      name.length < 1 ||
      name.length > 128 ||
      !TOOL_NAME_PATTERN.test(name) ||
      seen.has(name)
    ) {
      throw new Error('DSH emitted an invalid or duplicate tool name')
    }
    if (typeof description !== 'string' || description.length > 16_384) {
      throw new Error(`DSH tool ${name} emitted an invalid description`)
    }
    if (parameters === null || typeof parameters !== 'object' || Array.isArray(parameters)) {
      throw new Error(`DSH tool ${name} emitted an invalid parameters schema`)
    }
    seen.add(name)
    const detached = JSON.parse(JSON.stringify({
      name,
      description: Array.from(description).slice(0, 1024).join(''),
      inputSchema: parameters,
    }))
    assertUnicodeScalars(detached)
    if (byteLength(detached) > maximumBytes) {
      throw new Error(`DSH tool ${name} schema exceeds the configured limit`)
    }
    return detached
  })
  tools.sort((left, right) =>
    Buffer.compare(Buffer.from(left.name, 'utf8'), Buffer.from(right.name, 'utf8')))
  if (byteLength(tools) > maximumBytes) {
    throw new Error('DSH tool inventory exceeds the configured limit')
  }
  return tools
}

function textContent(text) {
  return { type: 'text', text: String(text) }
}

function projectContent(content) {
  if (!Array.isArray(content)) return []
  return content.map((block) => {
    if (block !== null && typeof block === 'object' && !Array.isArray(block)) {
      if (block.type === 'text' && typeof block.text === 'string') return textContent(block.text)
      if (block.type === 'reasoning' && typeof block.text === 'string') return textContent(block.text)
      const kind = typeof block.type === 'string' ? block.type : 'unknown'
      return textContent(`[DSH ${kind} content is not representable on this MCP bridge]`)
    }
    return textContent('[DSH emitted an invalid content block]')
  })
}

function toolFailure(message, code = 'DSH_TOOL_BRIDGE_ERROR') {
  return {
    content: [textContent(`Error: ${message}`)],
    isError: true,
    _meta: { 'io.ksadk/dsh': { code } },
  }
}

function projectToolResult(result, maximumBytes) {
  if (result === null || typeof result !== 'object' || Array.isArray(result)) {
    return toolFailure('DSH returned an invalid tool result')
  }
  const content = projectContent(result.content)
  const projected = {
    content,
    isError: result.isError === true,
  }
  if (result.isError === true) {
    const failure = result.error
    const message = failure && typeof failure.message === 'string'
      ? failure.message.slice(0, 16_384)
      : 'DSH tool execution failed'
    if (projected.content.length === 0) projected.content.push(textContent(`Error: ${message}`))
    const code = failure?.info && typeof failure.info.code === 'string'
      ? failure.info.code.slice(0, 256)
      : 'DSH_TOOL_ERROR'
    projected._meta = { 'io.ksadk/dsh': { code } }
  } else if (
    result.value !== null &&
    typeof result.value === 'object' &&
    !Array.isArray(result.value)
  ) {
    projected.structuredContent = result.value
  } else if (projected.content.length === 0 && result.value !== undefined) {
    projected.content.push(textContent(JSON.stringify(result.value)))
  }
  try {
    if (byteLength(projected) <= maximumBytes) return projected
  } catch {}
  return toolFailure('DSH tool result exceeds the configured bridge limit', 'DSH_RESULT_TOO_LARGE')
}

function requestKey(scopeId, id) {
  return JSON.stringify([scopeId, typeof id, id])
}

function abortOutcome(controller) {
  return new Promise((resolve) => {
    const finish = () => {
      const reason = controller.signal.reason
      resolve({
        kind: 'aborted',
        code: reason?.code === 'DEADLINE_EXCEEDED' ? 'DEADLINE_EXCEEDED' : 'ABORTED',
      })
    }
    if (controller.signal.aborted) finish()
    else controller.signal.addEventListener('abort', finish, { once: true })
  })
}

function validateRequestEnvelope(message) {
  return message !== null &&
    typeof message === 'object' &&
    !Array.isArray(message) &&
    message.jsonrpc === '2.0' &&
    typeof message.method === 'string' &&
    (
      message.id === undefined ||
      typeof message.id === 'string' ||
      (typeof message.id === 'number' && Number.isSafeInteger(message.id))
    )
}

function normalizedProtocol(requested) {
  return SUPPORTED_MCP_PROTOCOLS.has(requested) ? requested : MCP_PROTOCOL_VERSION
}

async function listen(server) {
  await new Promise((resolve, reject) => {
    const onError = (error) => {
      server.off('listening', onListening)
      reject(error)
    }
    const onListening = () => {
      server.off('error', onError)
      resolve()
    }
    server.once('error', onError)
    server.once('listening', onListening)
    server.listen(0, '127.0.0.1')
  })
}

async function closeServer(server, pending) {
  for (const entry of pending.values()) entry.controller.abort(new Error('DSH capability host is stopping'))
  const closed = new Promise((resolve) => server.close(resolve))
  const timer = new Promise((resolve) => setTimeout(resolve, 2_000))
  await Promise.race([closed, timer])
  server.closeAllConnections?.()
}

function emitReadyRecord(record) {
  process.stdout.write(`@@KSADK_DSH_CAPABILITY_READY@@${JSON.stringify(record)}\n`)
}

function emitRuntimeToken(token) {
  process.stdout.write(`@@KSADK_DSH_CAPABILITY_TOKEN@@${token}\n`)
}

export async function apply(ctx, config = {}) {
  const profile = requiredEnvironment('KSADK_DSH_PROFILE', 64)
  const profileDigest = requiredEnvironment('KSADK_DSH_PROFILE_DIGEST', 80)
  const dshVersion = requiredEnvironment('KSADK_DSH_VERSION', 64)
  requiredEnvironment('KSADK_DSH_CAPABILITY_READY_STDOUT', 8)
  const token = randomBytes(48).toString('base64url')
  if (!DIGEST_PATTERN.test(profileDigest)) throw new Error('KSADK_DSH_PROFILE_DIGEST is invalid')

  const callTimeoutMs = positiveInteger(config.callTimeoutMs, DEFAULT_CALL_TIMEOUT_MS, 'callTimeoutMs', 3_600_000)
  const maxArgumentBytes = positiveInteger(config.maxArgumentBytes, DEFAULT_MAX_ARGUMENT_BYTES, 'maxArgumentBytes', 8 * 1024 * 1024)
  const maxResultBytes = positiveInteger(config.maxResultBytes, DEFAULT_MAX_RESULT_BYTES, 'maxResultBytes', 8 * 1024 * 1024)
  const maxRequestBytes = positiveInteger(config.maxRequestBytes, DEFAULT_MAX_REQUEST_BYTES, 'maxRequestBytes', 8 * 1024 * 1024)
  const maxInFlight = positiveInteger(config.maxInFlight, DEFAULT_MAX_IN_FLIGHT, 'maxInFlight', 1024)
  const inventoryQuietMs = positiveInteger(config.inventoryQuietMs, 50, 'inventoryQuietMs', 5_000)

  const pending = new Map()
  const revokedScopes = new Map()
  let draining = false
  let drifted = false
  let readyWritten = false
  let lastToolChange = Date.now()
  let tools = []
  let inventoryDigest = digest(tools)

  ctx.on('tools/change', () => {
    lastToolChange = Date.now()
    if (readyWritten) drifted = true
  }, { global: true })

  const server = createServer(async (request, response) => {
    response.setHeader('X-Content-Type-Options', 'nosniff')
    if (!originAllowed(request)) {
      writeJson(response, 403, { error: 'origin_denied' })
      return
    }
    const authorization = authorize(request, token, profileDigest, tools, revokedScopes)
    if (authorization === null) {
      response.setHeader('WWW-Authenticate', 'Bearer')
      writeJson(response, 401, { error: 'unauthorized' })
      return
    }

    let parsedUrl
    try {
      parsedUrl = new URL(request.url ?? '/', 'http://127.0.0.1')
    } catch {
      writeJson(response, 400, { error: 'invalid_url' })
      return
    }

    if (request.method === 'GET' && parsedUrl.pathname === '/health' && parsedUrl.search === '') {
      writeJson(response, drifted || draining ? 503 : 200, {
        protocolVersion: HOST_PROTOCOL,
        healthy: !drifted && !draining,
        profile,
        profileDigest,
        inventoryDigest,
        toolCount: tools.length,
        inFlight: pending.size,
        drifted,
        draining,
      })
      return
    }
    if (request.method !== 'POST' || parsedUrl.pathname !== '/mcp' || parsedUrl.search !== '') {
      writeJson(response, 404, { error: 'not_found' })
      return
    }
    if (!(request.headers['content-type'] ?? '').toLowerCase().startsWith('application/json')) {
      writeJson(response, 415, { error: 'content_type_required' })
      return
    }

    let message
    try {
      const body = await readBody(request, maxRequestBytes)
      message = JSON.parse(body)
    } catch (error) {
      if (error instanceof RequestTooLargeError) {
        writeJson(response, 413, { error: 'request_too_large' })
      } else {
        writeJson(response, 400, jsonRpcError(null, -32700, 'Parse error'))
      }
      return
    }
    if (!validateRequestEnvelope(message)) {
      writeJson(response, 400, jsonRpcError(message?.id, -32600, 'Invalid Request'))
      return
    }
    // A scoped request may have been authorized before a slow body finished
    // while its activation was revoked by a concurrent root request.
    if (!authorization.root && revokedScopes.has(authorization.scopeId)) {
      response.setHeader('WWW-Authenticate', 'Bearer')
      writeJson(response, 401, { error: 'unauthorized' })
      return
    }

    const isNotification = message.id === undefined
    const params = message.params ?? {}
    if (params === null || typeof params !== 'object' || Array.isArray(params)) {
      if (isNotification) writeEmpty(response, 202)
      else writeJson(response, 200, jsonRpcError(message.id, -32602, 'Invalid params'))
      return
    }

    if (message.method === 'notifications/initialized') {
      writeEmpty(response, 202)
      return
    }
    if (message.method === 'notifications/cancelled') {
      const error = new Error('MCP client cancelled the request')
      error.code = 'ABORTED'
      if (authorization.root) {
        for (const active of pending.values()) {
          if (active.requestIdKey === requestKey('', params.requestId)) {
            active.controller.abort(error)
          }
        }
      } else {
        const active = pending.get(requestKey(authorization.scopeId, params.requestId))
        if (active) active.controller.abort(error)
      }
      writeEmpty(response, 202)
      return
    }
    if (isNotification) {
      writeEmpty(response, 202)
      return
    }

    if (message.method === 'initialize') {
      if (typeof params.protocolVersion !== 'string') {
        writeJson(response, 200, jsonRpcError(message.id, -32602, 'protocolVersion is required'))
        return
      }
      writeJson(response, 200, {
        jsonrpc: '2.0',
        id: message.id,
        result: {
          protocolVersion: normalizedProtocol(params.protocolVersion),
          capabilities: { tools: {} },
          serverInfo: { name: '@kingsoftcloud/ksadk-dsh-capability-host', version: HOST_VERSION },
        },
      })
      return
    }
    if (message.method === 'ping') {
      writeJson(response, 200, { jsonrpc: '2.0', id: message.id, result: {} })
      return
    }
    if (message.method === 'io.ksadk/scopes/revoke') {
      if (!authorization.root || typeof params.token !== 'string' || params.token.length > 12 * 1024) {
        writeJson(response, 200, jsonRpcError(message.id, -32602, 'valid scoped token is required'))
        return
      }
      const scoped = parseScopedToken(params.token, token, profileDigest, tools, true)
      if (scoped === null) {
        writeJson(response, 200, jsonRpcError(message.id, -32602, 'valid scoped token is required'))
        return
      }
      if (!revokedScopes.has(scoped.scopeId) && revokedScopes.size >= 4096) {
        writeJson(response, 200, jsonRpcError(message.id, -32001, 'revocation capacity reached'))
        return
      }
      revokedScopes.set(scoped.scopeId, scoped.expiresAt)
      const error = new Error('MCP credential scope was revoked')
      error.code = 'ABORTED'
      for (const active of pending.values()) {
        if (active.scopeId === scoped.scopeId) active.controller.abort(error)
      }
      writeJson(response, 200, {
        jsonrpc: '2.0',
        id: message.id,
        result: { revoked: true },
      })
      return
    }
    if (draining || drifted) {
      writeJson(response, 200, jsonRpcError(message.id, -32002, 'DSH capability inventory is unavailable'))
      return
    }
    if (message.method === 'tools/list') {
      if (params.cursor !== undefined && params.cursor !== null && params.cursor !== '') {
        writeJson(response, 200, jsonRpcError(message.id, -32602, 'cursor is not supported'))
        return
      }
      const visibleTools = authorization.aliases === null
        ? tools
        : [...authorization.aliases].map(([alias, source]) => {
          const tool = tools.find((item) => item.name === source)
          return { ...tool, name: alias }
        })
      writeJson(response, 200, { jsonrpc: '2.0', id: message.id, result: { tools: visibleTools } })
      return
    }
    if (message.method !== 'tools/call') {
      writeJson(response, 200, jsonRpcError(message.id, -32601, 'Method not found'))
      return
    }
    if (typeof params.name !== 'string') {
      writeJson(response, 200, {
        jsonrpc: '2.0',
        id: message.id,
        result: toolFailure('unknown DSH tool', 'UNKNOWN_TOOL'),
      })
      return
    }
    const sourceToolName = authorization.aliases === null
      ? params.name
      : authorization.aliases.get(params.name)
    if (sourceToolName === undefined) {
      writeJson(response, 200, jsonRpcError(message.id, -32003, 'Tool is outside credential scope'))
      return
    }
    if (!tools.some((tool) => tool.name === sourceToolName)) {
      writeJson(response, 200, {
        jsonrpc: '2.0',
        id: message.id,
        result: toolFailure('unknown DSH tool', 'UNKNOWN_TOOL'),
      })
      return
    }
    const argumentsValue = params.arguments ?? {}
    if (argumentsValue === null || typeof argumentsValue !== 'object' || Array.isArray(argumentsValue)) {
      writeJson(response, 200, jsonRpcError(message.id, -32602, 'tool arguments must be an object'))
      return
    }
    try {
      if (byteLength(argumentsValue) > maxArgumentBytes) {
        writeJson(response, 200, jsonRpcError(message.id, -32602, 'tool arguments exceed the configured limit'))
        return
      }
    } catch {
      writeJson(response, 200, jsonRpcError(message.id, -32602, 'tool arguments must be JSON'))
      return
    }
    if (pending.size >= maxInFlight) {
      writeJson(response, 200, jsonRpcError(message.id, -32001, 'too many in-flight tool calls'))
      return
    }

    const key = requestKey(authorization.scopeId, message.id)
    if (pending.has(key)) {
      writeJson(response, 200, jsonRpcError(message.id, -32600, 'duplicate in-flight request id'))
      return
    }
    const controller = new AbortController()
    const timeout = setTimeout(() => {
      const error = new Error('DSH tool call deadline exceeded')
      error.code = 'DEADLINE_EXCEEDED'
      controller.abort(error)
    }, callTimeoutMs)
    const completion = Promise.resolve()
      .then(() => ctx.tools.execute({
        callId: `mcp-${randomUUID()}`,
        name: sourceToolName,
        arguments: argumentsValue,
        signal: controller.signal,
      }))
      .then(
        (result) => ({ kind: 'result', result }),
        (error) => ({ kind: 'error', error }),
      )
    pending.set(key, {
      controller,
      completion,
      scopeId: authorization.scopeId,
      requestIdKey: requestKey('', message.id),
    })
    response.on('close', () => {
      if (!response.writableEnded) {
        const error = new Error('MCP client disconnected')
        error.code = 'ABORTED'
        controller.abort(error)
      }
    })
    let completed = false
    try {
      const outcome = await Promise.race([completion, abortOutcome(controller)])
      completed = outcome.kind !== 'aborted'
      if (outcome.kind === 'result') {
        writeJson(response, 200, {
          jsonrpc: '2.0',
          id: message.id,
          result: projectToolResult(outcome.result, maxResultBytes),
        })
      } else if (outcome.kind === 'aborted') {
        const messageText = outcome.code === 'DEADLINE_EXCEEDED'
          ? 'DSH tool call deadline exceeded'
          : 'DSH tool call was cancelled'
        writeJson(response, 200, {
          jsonrpc: '2.0',
          id: message.id,
          result: toolFailure(messageText, outcome.code),
        })
      } else {
        writeJson(response, 200, jsonRpcError(message.id, -32603, 'DSH tool pipeline failed'))
      }
    } finally {
      clearTimeout(timeout)
      if (completed) pending.delete(key)
      else {
        void completion.then(() => pending.delete(key))
        // AbortSignal is cooperative. A third-party Cordis tool may ignore it
        // forever, so an aborted call cannot be allowed to retain one in-flight
        // slot indefinitely. Give a cooperative tool a short unwind window;
        // otherwise crash this isolated generation and let the Python
        // supervisor reap the process group and start a clean generation.
        const abortGrace = setTimeout(() => {
          if (!pending.has(key)) return
          draining = true
          server.closeAllConnections?.()
          process.exit(70)
        }, 250)
        abortGrace.unref()
        void completion.then(() => clearTimeout(abortGrace))
      }
    }
  })
  server.maxHeadersCount = 64
  server.headersTimeout = 10_000
  server.requestTimeout = callTimeoutMs + 10_000
  server.keepAliveTimeout = 5_000
  server.on('clientError', (_error, socket) => socket.destroy())

  let stopped = false
  const stop = async () => {
    if (stopped) return
    stopped = true
    draining = true
    await closeServer(server, pending)
  }
  ctx.effect(() => stop)

  try {
    await listen(server)
    while (Date.now() - lastToolChange < inventoryQuietMs) {
      await new Promise((resolve) => setTimeout(resolve, inventoryQuietMs))
    }
    tools = snapshotTools(ctx.tools, maxRequestBytes)
    inventoryDigest = digest(tools)
    const address = server.address()
    if (address === null || typeof address === 'string' || address.address !== '127.0.0.1') {
      throw new Error('capability host did not bind a loopback TCP address')
    }
    readyWritten = true
    emitRuntimeToken(token)
    emitReadyRecord({
      protocolVersion: HOST_PROTOCOL,
      hostVersion: HOST_VERSION,
      dshVersion,
      profile,
      profileDigest,
      definition: 'mcp.connector/v1',
      transport: 'streamable-http',
      endpoint: `http://127.0.0.1:${address.port}/mcp`,
      inventoryDigest,
      tools,
    })
  } catch (error) {
    await stop()
    throw error
  }
}
