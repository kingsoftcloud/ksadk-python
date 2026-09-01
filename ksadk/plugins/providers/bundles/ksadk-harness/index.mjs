const contribution = Object.freeze({
  ecosystem: 'dsh',
  definition: 'agent.provider/v1',
  slot: 'agent.execution',
  providerId: 'io.ksadk.harness-provider',
  providerVersion: '1.0.0',
  displayName: 'KsADK Harness',
  runtimeProtocols: Object.freeze(['agentkit.runtime/v1']),
})

export const name = 'ksadk-harness-provider'

export function apply(ctx) {
  // The DSH Profile owns discovery and lifecycle. KsADK consumes this
  // contribution through its frozen provider-host bridge; the existing
  // RuntimeAdapter remains the execution backend during migration.
  ctx.provide('ksadkAgentProvider', contribution)
  ctx.effect(() => () => {})
}
