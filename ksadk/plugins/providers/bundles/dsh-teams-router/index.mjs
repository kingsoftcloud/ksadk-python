export const name = 'dsh-teams-router'
export const inject = ["pluginCompanions", "teamsTasks", "teamsProfile"]

export async function apply(ctx) {
  const dispose = await ctx.pluginCompanions.registerComponent('io.ksadk.teams', 'dsh-teams-router')
  ctx.effect(() => dispose)
  ctx.provide('teamsRouter', Object.freeze({
    pluginId: 'io.ksadk.teams', protocol: 'teams.ksadk.io/v1',
    generationId: ctx.pluginCompanions.generationId,
    call(operation, args, options) {
      return ctx.teamsTasks.operations.includes(operation)
        ? ctx.teamsTasks.call(operation, args, options)
        : ctx.teamsProfile.call(operation, args, options)
    },
  }))
}
