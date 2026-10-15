export const name = 'dsh-knowledge'
export const inject = ['tools', 'platformResources']

// The platform retrieval service may prefix a chunk with its internal object
// storage key (for example ``upload_files/<numeric-id>/<uuid>.md,``).  That
// value is neither useful evidence nor safe user-facing content.  Long numeric
// object keys also look like payment-card numbers to the Harness DLP policy,
// which forces an otherwise harmless knowledge result into an unreadable
// sensitive artifact.  Strip only this well-known transport prefix and retain
// the document title and every byte of authored content that follows it.
export function sanitizeKnowledgeContent(value) {
  if (typeof value !== 'string') return value
  return value.replace(/^upload_files\/[^,\r\n]+,/, '')
}

export function apply(ctx) {
  ctx.effect(() => ctx.platformResources.registerTool('search_knowledge_base', 'search_knowledge_base'))
  ctx.tools.register({
    name: 'search_knowledge_base',
    description: 'Search the knowledge base bound to this Agent and return document sources.',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'Non-empty query, at most 16000 characters.' },
        top_k: { type: 'integer', description: 'Positive result count, capped by the binding policy.' },
      },
      required: ['query'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          status: { type: 'string', enum: ['ok', 'empty', 'failed'] },
          bindingId: { type: 'string' },
          items: { type: 'array', items: {
            type: 'object',
            properties: {
              documentId: { type: 'string' }, documentName: { type: 'string' },
              segmentId: { type: 'string' }, content: { type: 'string' }, score: { type: 'number' },
            },
            required: ['documentId', 'documentName', 'segmentId', 'content', 'score'],
            additionalProperties: false,
          } },
          truncated: { type: 'boolean' }, requestId: { type: 'string' },
          errorCode: { oneOf: [
            { type: 'string', enum: ['KNOWLEDGE_RETRIEVAL_FAILED', 'RESOURCE_FORBIDDEN'] }, { type: 'null' },
          ] },
        },
        required: ['status', 'bindingId', 'items', 'truncated', 'requestId'],
        additionalProperties: false,
      },
      render(_args, result) {
        const text = result.status === 'failed' ? 'Knowledge retrieval failed.'
          : result.status === 'empty' ? 'No matching knowledge found.'
          : result.items.map(item => `[${item.documentName || item.documentId}] ${item.content}`).join('\n\n')
        return [{ type: 'text', text }]
      },
    },
    isConcurrencySafe: () => true,
    async execute(args, execution) {
      const result = await ctx.platformResources.call(
        'search_knowledge_base', args, { signal: execution.signal },
      )
      if (!result || !Array.isArray(result.items)) return result
      return {
        ...result,
        items: result.items.map(item => ({
          ...item,
          content: sanitizeKnowledgeContent(item.content),
        })),
      }
    },
  })
}
