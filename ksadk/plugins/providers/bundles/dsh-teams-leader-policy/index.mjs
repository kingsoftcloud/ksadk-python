export const name = 'dsh-teams-leader-policy'
export const inject = ["pluginCompanions", "teamsTools"]

export async function apply(ctx) {
  const dispose = await ctx.pluginCompanions.registerComponent('io.ksadk.teams', 'dsh-teams-leader-policy')
  ctx.effect(() => dispose)
  ctx.provide('teamsLeaderPolicy', Object.freeze({
    pluginId: 'io.ksadk.teams', protocol: 'teams.ksadk.io/v1',
    generationId: ctx.pluginCompanions.generationId,
    executionPolicy: 'ksadk.execution-policy/v1',
    authority: 'python-companion',
  }))
}
