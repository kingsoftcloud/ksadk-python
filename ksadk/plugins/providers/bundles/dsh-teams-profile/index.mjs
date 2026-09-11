export const name = 'dsh-teams-profile'
export const inject = ["pluginCompanions"]

export async function apply(ctx) {
  const dispose = await ctx.pluginCompanions.registerComponent('io.ksadk.teams', 'dsh-teams-profile')
  ctx.effect(() => dispose)
  ctx.provide('teamsProfile', Object.freeze({
    pluginId: 'io.ksadk.teams', protocol: 'teams.ksadk.io/v1',
    generationId: ctx.pluginCompanions.generationId,
    call: (operation, args, options) => ctx.pluginCompanions.call(operation, args, options),
  }))
}
