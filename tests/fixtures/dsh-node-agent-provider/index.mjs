import { appendFileSync } from 'node:fs'
import { createHash } from 'node:crypto'

const PACKAGE_NAME = '@ksadk-test/dsh-node-agent-provider'
const METHODS = [
  'activate',
  'cancel',
  'describe',
  'dispose',
  'drain',
  'execute',
  'handshake',
  'health',
  'inventory',
  'preflight',
]

function record(event) {
  const target = process.env.KSADK_DSH_EVENT_LOG
  if (target) appendFileSync(target, `${event}\n`, 'utf8')
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

function digest(value) {
  return `sha256:${createHash('sha256').update(canonical(value)).digest('hex')}`
}

function assertDigest(actual, expected, label) {
  if (actual !== expected) throw new Error(`${label} crossed its immutable digest fence`)
}

function createProvider() {
  let profile
  let descriptor
  let descriptorDigest
  let providerState = 'ready'
  let nextActivation = 0
  const activations = new Map()

  function requireProfile() {
    if (!profile || !descriptor || !descriptorDigest) throw new Error('profile handshake is incomplete')
  }

  function requireActivation(activationId) {
    const activation = activations.get(activationId)
    if (!activation) throw new Error('activation is unavailable')
    return activation
  }

  return {
    handshake(projection) {
      if (!projection.bundles.includes(PACKAGE_NAME)) {
        throw new Error('provider Bundle is not active in the selected DSH Profile')
      }
      if (profile && profile.configDigest !== projection.configDigest) {
        throw new Error('host cannot cross a DSH Profile digest fence')
      }
      profile = projection
      descriptor = {
        descriptorFormat: 'dsh.agent-provider-descriptor/v1',
        ecosystem: 'dsh',
        providerId: 'io.ksadk.test.dsh-node-provider',
        providerVersion: '1.0.0',
        displayName: 'DSH Cordis Node provider',
        pluginName: PACKAGE_NAME,
        profile: profile.profile,
        profileDigest: profile.configDigest,
        definition: 'agent.provider/v1',
        slot: 'agent.execution',
        runtimeProtocols: ['agentkit.runtime/v1'],
      }
      descriptorDigest = digest(descriptor)
      record(`cordis:profile:${profile.configDigest}`)
      return {
        protocolVersion: 'ksadk.dsh-agent-provider-host/v1',
        methods: METHODS,
        hostVersion: '1.0.0',
      }
    },

    describe(projection) {
      requireProfile()
      assertDigest(projection.configDigest, profile.configDigest, 'profile')
      return descriptor
    },

    preflight(params) {
      requireProfile()
      assertDigest(params.profileDigest, profile.configDigest, 'profile')
      assertDigest(params.descriptorDigest, descriptorDigest, 'descriptor')
      return {
        ready: providerState === 'ready',
        descriptorDigest,
        profileDigest: profile.configDigest,
      }
    },

    activate(params) {
      requireProfile()
      assertDigest(params.descriptorDigest, descriptorDigest, 'descriptor')
      const activationId = `cordis-node-${++nextActivation}`
      activations.set(activationId, {
        agentId: params.bundle.manifest.agentId,
        turns: [],
        cancelled: false,
        drained: false,
      })
      record(`cordis:activate:${activationId}`)
      return { activationId }
    },

    inventory(params) {
      requireProfile()
      assertDigest(params.descriptorDigest, descriptorDigest, 'descriptor')
      return {
        providerId: descriptor.providerId,
        providerVersion: descriptor.providerVersion,
        profile: profile.profile,
        profileDigest: profile.configDigest,
        descriptorDigest,
        state: providerState,
        activationCount: activations.size,
      }
    },

    health(params) {
      if (params.activationId) {
        const activation = activations.get(params.activationId)
        return { healthy: Boolean(activation && !activation.drained) }
      }
      return { healthy: providerState === 'ready' }
    },

    execute(params) {
      const activation = requireActivation(params.activationId)
      if (activation.drained) throw new Error('activation is draining')
      activation.turns.push(params.request)
      record(`cordis:execute:${params.activationId}:${activation.turns.length}`)
      const latestMessage = params.request?.messages?.at(-1)?.content ?? params.request?.message ?? 'turn'
      return {
        provider: 'dsh-cordis-node',
        agentId: activation.agentId,
        turn: activation.turns.length,
        history: [...activation.turns],
        cancelled: activation.cancelled,
        outputText: `${latestMessage}:turn-${activation.turns.length}`,
      }
    },

    cancel(params) {
      const activation = requireActivation(params.activationId)
      activation.cancelled = true
      record(`cordis:cancel:${params.activationId}`)
      return { ok: true }
    },

    drain(params) {
      if (params.scope === 'activation') {
        requireActivation(params.activationId).drained = true
      } else {
        providerState = 'draining'
      }
      record(`cordis:drain:${params.scope}`)
      return { ok: true }
    },

    dispose(params) {
      if (params.scope === 'activation') {
        activations.delete(params.activationId)
        record(`cordis:dispose:${params.activationId}`)
      }
      return { ok: true }
    },

    disposeAll() {
      providerState = 'disposed'
      activations.clear()
    },
  }
}

export const name = 'ksadk-test-dsh-agent-provider'

export function apply(ctx) {
  const provider = createProvider()
  ctx.provide('ksadkAgentProvider', provider)
  ctx.effect(() => {
    record('cordis:effect:active')
    return () => {
      provider.disposeAll()
      record('cordis:effect:disposed')
    }
  })
}
