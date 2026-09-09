export const name = 'dsh-memory'
export const inject = ['tools', 'platformResources']

export function apply(ctx) {
  for (const operation of ['update_memory', 'delete_memory']) {
    const updating = operation === 'update_memory'
    ctx.effect(() => ctx.platformResources.registerTool(operation, operation))
    ctx.tools.register({
      name: operation,
      description: updating
        ? 'Update a real memory ID recalled in this activation, after host approval. No versioned CAS is supported. Use only the returned new ID; if absent, search again.'
        : 'Soft-delete a real memory ID recalled in this activation, after host approval. This does not guarantee physical erasure.',
      parameters: {
        type: 'object', properties: {
          memoryId: { type: 'string', minLength: 1, maxLength: 256 },
          ...(updating ? { content: { type: 'string', minLength: 1, maxLength: 8192 } } : {}),
        },
        required: updating ? ['memoryId', 'content'] : ['memoryId'], additionalProperties: false,
      },
      output: {
        schema: {
          type: 'object', properties: {
            status: { type: 'string', enum: ['succeeded', 'failed', 'unknown'] },
            operationId: { type: 'string' }, memoryId: { type: 'string' },
            newMemoryId: { oneOf: [{ type: 'string' }, { type: 'null' }] },
            errorCode: { oneOf: [{ type: 'string' }, { type: 'null' }] },
          },
          required: ['status', 'operationId', 'memoryId', 'newMemoryId', 'errorCode'],
          additionalProperties: false,
        },
        render(_args, result) {
          const text = result.status === 'unknown'
            ? 'Memory mutation outcome is unknown. Do not submit it again automatically.'
            : result.status === 'failed' ? 'Memory mutation was not completed. Search again before proposing another change.'
            : !updating ? 'Memory soft-deletion confirmed. This receipt does not prove physical erasure.'
            : result.newMemoryId ? `Memory update confirmed. Returned record ID: ${result.newMemoryId}.`
            : 'Memory update confirmed, but its resulting record ID is unavailable. Search again before another change.'
          return [{ type: 'text', text }]
        },
      },
      isConcurrencySafe: () => false,
      async execute(args, execution) {
        const result = await ctx.platformResources.call(operation, args, { signal: execution.signal })
        if (!['succeeded', 'failed', 'unknown'].includes(result.status)
          || !/^[0-9a-f]{64}$/.test(result.operationId)
          || (result.memoryId !== undefined && result.memoryId !== args.memoryId)
          || (result.newMemoryId !== undefined && typeof result.newMemoryId !== 'string')
          || (result.status !== 'succeeded' && result.newMemoryId)
          || (!updating && result.newMemoryId)
          || (result.errorCode != null && ![
            'MEMORY_MUTATION_UNKNOWN', 'MEMORY_RECORD_NOT_OBSERVED', 'MEMORY_RECORD_NOT_FOUND',
          ].includes(result.errorCode))) throw new Error('MEMORY_RESPONSE_INVALID')
        return {
          status: result.status, operationId: result.operationId,
          memoryId: args.memoryId, newMemoryId: result.newMemoryId || null,
          errorCode: result.errorCode ?? (result.status === 'unknown' ? 'MEMORY_MUTATION_UNKNOWN' : null),
        }
      },
    })
  }
  ctx.effect(() => ctx.platformResources.registerTool('memory_status', 'memory_status'))
  ctx.tools.register({
    name: 'memory_status',
    description: 'Check an owned memory submission receipt without resubmitting it. Extraction completion does not prove search indexing is ready.',
    parameters: {
      type: 'object', properties: { operationId: { type: 'string', pattern: '^[0-9a-f]{64}$' } },
      required: ['operationId'], additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object', properties: {
          operationId: { type: 'string' }, status: { type: 'string' },
          extractionStatus: { type: 'string' }, searchable: { type: 'boolean' },
          errorCode: { oneOf: [{ type: 'string' }, { type: 'null' }] },
        },
        required: ['operationId', 'status', 'extractionStatus', 'searchable', 'errorCode'],
        additionalProperties: false,
      },
      render(_args, result) {
        return [{ type: 'text', text: `Memory extraction: ${result.extractionStatus}. Search indexing has not been confirmed.` }]
      },
    },
    isConcurrencySafe: () => true,
    execute(args, execution) {
      return ctx.platformResources.call('memory_status', args, { signal: execution.signal })
    },
  })
  ctx.effect(() => ctx.platformResources.registerTool('save_memory', 'save_memory'))
  ctx.tools.register({
    name: 'save_memory',
    description: 'Submit an explicitly authorized memory for this user. Acceptance is pending extraction, not confirmation that it can be recalled.',
    parameters: {
      type: 'object', properties: { content: { type: 'string', minLength: 1, maxLength: 8192 } },
      required: ['content'], additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object', properties: {
          status: { type: 'string', enum: ['accepted_pending', 'unknown', 'searchable', 'failed'] },
          operationId: { type: 'string' }, searchable: { type: 'boolean' },
        },
        required: ['status', 'operationId', 'searchable'], additionalProperties: false,
      },
      render(_args, result) {
        const text = result.status === 'accepted_pending' ? 'Memory accepted; extraction is pending.'
          : result.status === 'searchable' ? 'Memory indexing confirmed.'
          : result.status === 'failed' ? 'Memory submission failed.'
          : 'Memory submission outcome is unknown. Do not submit it again automatically.'
        return [{ type: 'text', text }]
      },
    },
    isConcurrencySafe: () => false,
    async execute(args, execution) {
      const result = await ctx.platformResources.call('save_memory', args, { signal: execution.signal })
      if (!['accepted_pending', 'unknown', 'searchable', 'failed'].includes(result.status)
        || typeof result.operationId !== 'string') throw new Error('MEMORY_RESPONSE_INVALID')
      return {
        status: result.status, operationId: result.operationId,
        searchable: result.status === 'searchable',
      }
    },
  })
  ctx.effect(() => ctx.platformResources.registerTool('load_memory', 'load_memory'))
  ctx.tools.register({
    name: 'load_memory',
    description: 'Recall memories for the authenticated user from this Agent’s bound memory instance.',
    parameters: {
      type: 'object',
      properties: { query: { type: 'string', maxLength: 16000 } },
      required: ['query'], additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          status: { type: 'string', enum: ['ok', 'empty', 'failed', 'unauthorized'] },
          items: { type: 'array', items: {
            type: 'object',
            properties: {
              memoryId: { type: 'string' }, content: { type: 'string' },
              score: { oneOf: [{ type: 'number' }, { type: 'null' }] },
              version: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
            },
            required: ['memoryId', 'content', 'score', 'version'], additionalProperties: false,
          } },
          truncated: { type: 'boolean' },
          errorCode: { oneOf: [{ type: 'string' }, { type: 'null' }] },
        },
        required: ['status', 'items', 'truncated', 'errorCode'], additionalProperties: false,
      },
      render(_args, result) {
        const text = result.status === 'empty' ? 'No matching memories found.'
          : result.status !== 'ok' ? 'Memory retrieval unavailable.'
          : result.items.map(item => `[${item.memoryId}] ${item.content}`).join('\n\n')
        return [{ type: 'text', text }]
      },
    },
    isConcurrencySafe: () => true,
    async execute(args, execution) {
      const result = await ctx.platformResources.call('load_memory', args, { signal: execution.signal })
      if (!['ok', 'failed', 'unauthorized'].includes(result.status) || !Array.isArray(result.records)) {
        throw new Error('MEMORY_RESPONSE_INVALID')
      }
      // Tenant/user partition and internal provenance remain host-side.
      const items = result.status === 'ok' ? result.records.map(record => ({
        memoryId: record.memory_id, content: record.content,
        score: record.metadata?.score ?? null, version: record.version ?? null,
      })) : []
      return {
        status: result.status === 'ok' && items.length === 0 ? 'empty' : result.status,
        items, truncated: Boolean(result.truncated_by_budget), errorCode: result.error_code ?? null,
      }
    },
  })
}
