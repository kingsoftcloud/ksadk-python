import { CompanionBridge } from './client.mjs'

export const name = 'ksadk-plugin-companions'

// Private supervisor material never enters Loader entry.config or the client
// boot graph. Remove it before plugins can launch delegated child processes.
const configuration = JSON.parse(process.env.KSADK_DSH_COMPANION_CONFIGURATION || 'null')
delete process.env.KSADK_DSH_COMPANION_CONFIGURATION

export function apply(ctx) {
  const bridge = new CompanionBridge(configuration)
  ctx.provide('pluginCompanions', bridge)
  ctx.effect(() => () => bridge.dispose())
}
