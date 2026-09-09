// Ordinary third-party-shaped fixture: a field named status has no resource semantics.
export const name = 'plain-status-fixture'
export const inject = ['tools']
export function apply(ctx) {
  ctx.tools.register({
    name: 'plain_status', description: 'Return an ordinary status value for projection tests.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: {
      schema: { type: 'object', properties: { status: { type: 'string' } }, required: ['status'] },
      render(_args, value) { return [{ type: 'text', text: value.status }] },
    },
    isConcurrencySafe: () => true,
    execute() { return { status: 'failed' } },
  })
}
