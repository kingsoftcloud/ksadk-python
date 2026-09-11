export const name = 'dsh-teams-client'
export const inject = ["pluginCompanions", "teamsLeaderPolicy"]

export async function apply(ctx) {
  const dispose = await ctx.pluginCompanions.registerComponent('io.ksadk.teams', 'dsh-teams-client')
  ctx.effect(() => dispose)
  ctx.provide('teamsClient', Object.freeze({
    pluginId: 'io.ksadk.teams', protocol: 'teams.ksadk.io/v1',
    generationId: ctx.pluginCompanions.generationId,
  }))
}
