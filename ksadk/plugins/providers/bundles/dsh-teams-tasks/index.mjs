export const name = 'dsh-teams-tasks'
export const inject = ["pluginCompanions", "teamsProfile"]

export async function apply(ctx) {
  const dispose = await ctx.pluginCompanions.registerComponent('io.ksadk.teams', 'dsh-teams-tasks')
  ctx.effect(() => dispose)
  ctx.provide('teamsTasks', Object.freeze({
    pluginId: 'io.ksadk.teams', protocol: 'teams.ksadk.io/v1',
    generationId: ctx.pluginCompanions.generationId,
    operations: Object.freeze(['team_create_task', 'team_task_action', 'team_wait', 'team_submit_result']),
    call(operation, args, options) {
      if (!this.operations.includes(operation)) throw new Error('COMPANION_OPERATION_INVALID')
      return ctx.teamsProfile.call(operation, args, options)
    },
  }))
}
