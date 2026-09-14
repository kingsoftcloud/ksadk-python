import { readFile } from 'node:fs/promises'
export const name = 'dsh-teams-tools'
export const inject = ["pluginCompanions", "teamsRouter", "tools"]

export async function apply(ctx) {
  const dispose = await ctx.pluginCompanions.registerComponent('io.ksadk.teams', 'dsh-teams-tools')
  ctx.effect(() => dispose)
  ctx.provide('teamsTools', Object.freeze({
    pluginId: 'io.ksadk.teams', protocol: 'teams.ksadk.io/v1',
    generationId: ctx.pluginCompanions.generationId,
  }))
  const definitions = JSON.parse(await readFile(new URL('./tools.json', import.meta.url), 'utf8'))
  for (const tool of definitions) {
    ctx.effect(() => ctx.pluginCompanions.registerTool(tool.name, 'io.ksadk.teams', tool.name))
    ctx.tools.register({
      ...tool,
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
      },
      isConcurrencySafe: () => tool.name === 'team_context',
      execute: (args, execution) => ctx.teamsRouter.call(tool.name, args, { signal: execution.signal }),
    })
  }
}
