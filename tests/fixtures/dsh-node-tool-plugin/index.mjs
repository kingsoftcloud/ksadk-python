import { appendFileSync } from 'node:fs'

export const name = 'ksadk-test-dsh-tool-plugin'
export const inject = ['tools']

function record(event) {
  const target = process.env.KSADK_DSH_FIXTURE_EVENT_LOG
  if (target) appendFileSync(target, `${event}\n`, 'utf8')
}

function wait(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, milliseconds)
    const abort = () => {
      clearTimeout(timer)
      reject(signal.reason ?? new Error('fixture call aborted'))
    }
    if (signal.aborted) abort()
    else signal.addEventListener('abort', abort, { once: true })
  })
}

export function apply(ctx) {
  ctx.tools.register({
    name: 'fixture_echo',
    description: 'Echo a message through the real DSH ToolRuntime pipeline.',
    parameters: {
      type: 'object',
      properties: {
        message: { type: 'string' },
        delayMs: { type: 'integer' },
      },
      required: ['message'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          message: { type: 'string' },
          callId: { type: 'string' },
        },
        required: ['message', 'callId'],
        additionalProperties: false,
      },
      render: (_args, value) => [{ type: 'text', text: value.message }],
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      if (args === null || typeof args !== 'object' || typeof args.message !== 'string') {
        throw new Error('message must be a string')
      }
      if (args.delayMs !== undefined) {
        if (!Number.isInteger(args.delayMs) || args.delayMs < 0 || args.delayMs > 60_000) {
          throw new Error('delayMs must be an integer between 0 and 60000')
        }
        await wait(args.delayMs, exec.signal)
      }
      record(`tool:execute:${exec.callId}`)
      return { message: args.message, callId: String(exec.callId) }
    },
  })
  record('tool:registered')
  ctx.effect(() => () => record('tool:disposed'))
}
