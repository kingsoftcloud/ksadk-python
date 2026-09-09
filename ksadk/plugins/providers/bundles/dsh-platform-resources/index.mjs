import { ResourceBridge } from './client.mjs'

export const name = 'dsh-platform-resources'

export function apply(ctx, config = {}) {
  const bridge = new ResourceBridge(config.socketPath)
  ctx.provide('platformResources', bridge)
  ctx.effect(() => () => bridge.dispose())
}
