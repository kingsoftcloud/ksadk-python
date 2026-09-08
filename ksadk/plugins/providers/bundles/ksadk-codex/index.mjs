const contribution = Object.freeze({
  ecosystem: 'dsh',
  definition: 'agent.provider/v1',
  slot: 'agent.execution',
  providerId: 'io.ksadk.codex-provider',
  providerVersion: '1.0.0',
  displayName: 'Codex',
  runtimeProtocols: Object.freeze(['agentkit.runtime/v1']),
})

export const name = 'ksadk-codex-provider'

export function apply(ctx) {
  // DSH owns package/Profile lifecycle. The fixed KsADK bridge retains the
  // existing Codex App Server backend and never executes package supplied argv.
  ctx.provide('ksadkAgentProvider', contribution)
  ctx.effect(() => () => {})
}
