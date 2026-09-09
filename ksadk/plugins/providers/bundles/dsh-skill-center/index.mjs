export const name = 'dsh-skill-center'
export const inject = ['tools', 'platformResources']

const skillId = { type: 'string', minLength: 1, maxLength: 256 }
const artifactProperties = {
  artifactId: { type: 'string' }, name: { type: 'string' },
  size: { type: 'integer' }, sha256: { type: 'string' },
}
const resourceOutput = {
  type: 'object', properties: {
    status: { type: 'string', enum: ['ok'] }, skillId: { type: 'string' },
    versionId: { type: 'string' }, contentHash: { type: 'string' }, path: { type: 'string' },
    content: { type: 'string' }, truncated: { type: 'boolean' },
    nextOffset: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
  },
  required: ['status', 'skillId', 'versionId', 'contentHash', 'path', 'content', 'truncated', 'nextOffset'],
  additionalProperties: false,
}

export function apply(ctx) {
  const definitions = [
    {
      name: 'execute_skills', description: 'Execute selected locked Skills in the Build’s isolated runtime after host approval. Discovery selections must first be loaded and recorded. An unknown outcome must not be retried as a new operation.',
      parameters: { type: 'object', properties: {
        workflowPrompt: { type: 'string', minLength: 1, maxLength: 16000 },
        skillIds: { type: 'array', items: skillId, minItems: 1, maxItems: 32, uniqueItems: true },
      }, required: ['workflowPrompt', 'skillIds'], additionalProperties: false },
      schema: { type: 'object', properties: {
        status: { type: 'string', enum: ['succeeded', 'unknown', 'failed'] },
        operationId: { type: 'string' }, replayed: { type: 'boolean' },
        artifacts: { type: 'array', items: { type: 'object', properties: artifactProperties,
          required: ['artifactId', 'name', 'size', 'sha256'], additionalProperties: false } },
      }, required: ['status', 'operationId'], additionalProperties: false },
      render: result => result.status === 'succeeded'
        ? `Skill execution completed. Operation: ${result.operationId}. Artifacts: ${(result.artifacts ?? []).map(item => `${item.name} (${item.artifactId})`).join(', ') || 'none'}`
        : `Skill execution outcome: ${result.status}. Operation: ${result.operationId}. Do not submit a new execution to recover this result.`,
    },
    {
      name: 'read_skill_artifact', description: 'Read a bounded base64 chunk of an artifact from an authorized Skill execution. Follow nextOffset to retrieve the complete file.',
      parameters: { type: 'object', properties: {
        operationId: { type: 'string', pattern: '^[0-9a-f]{64}$' },
        artifactId: { type: 'string', pattern: '^[0-9a-f]{64}$' },
        offset: { type: 'integer', minimum: 0, maximum: 20971520 },
        maxBytes: { type: 'integer', minimum: 1, maximum: 32768 },
      }, required: ['operationId', 'artifactId'], additionalProperties: false },
      schema: { type: 'object', properties: {
        ...artifactProperties, operationId: { type: 'string' }, encoding: { type: 'string', enum: ['base64'] },
        content: { type: 'string' }, offset: { type: 'integer' },
        nextOffset: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
      }, required: ['artifactId', 'name', 'size', 'sha256', 'operationId', 'encoding', 'content', 'offset', 'nextOffset'], additionalProperties: false },
      render: result => `${result.name} — base64 at byte ${result.offset}:\n${result.content}\nNext offset: ${result.nextOffset ?? 'end'}`,
    },
    {
      name: 'list_skills', description: 'List Skills allowed by this Agent binding: Build-locked packages in pinned mode, or the admitted space in discovery mode.',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
      schema: {
        type: 'object', properties: {
          status: { type: 'string', enum: ['ok'] }, truncated: { type: 'boolean' },
          items: { type: 'array', items: {
            type: 'object', properties: {
              skillId: { type: 'string' }, name: { type: 'string' }, versionId: { type: 'string' }, contentHash: { type: 'string' },
            }, required: ['skillId', 'name', 'versionId', 'contentHash'], additionalProperties: false,
          } },
        }, required: ['status', 'items', 'truncated'], additionalProperties: false,
      },
      render: result => result.items.map(item => `${item.name} (${item.skillId}@${item.versionId})`).join('\n') || 'No Skills are available.',
    },
    {
      name: 'search_skills', description: 'Search names and descriptions of Skills allowed by this Agent binding using metadata matching.',
      parameters: {
        type: 'object', properties: {
          query: { type: 'string', minLength: 1, maxLength: 16000 },
          maxResults: { type: 'integer', minimum: 1, maximum: 32 },
        }, required: ['query'], additionalProperties: false,
      },
      schema: {
        type: 'object', properties: {
          status: { type: 'string', enum: ['ok'] }, truncated: { type: 'boolean' },
          items: { type: 'array', items: {
            type: 'object', properties: {
              skillId: { type: 'string' }, name: { type: 'string' }, versionId: { type: 'string' },
              contentHash: { type: 'string' }, description: { type: 'string' },
              score: { type: 'integer' }, reason: { type: 'string' },
            }, required: ['skillId', 'name', 'versionId', 'contentHash', 'description', 'score', 'reason'],
            additionalProperties: false,
          } },
        }, required: ['status', 'items', 'truncated'], additionalProperties: false,
      },
      render: result => result.items.map(item => `${item.name} (${item.skillId}@${item.versionId}): ${item.description}`).join('\n') || 'No Skill matches this query.',
    },
    {
      name: 'load_skill', description: 'Read an allowed Skill’s SKILL.md and fix its version for this run when using discovery. Follow nextOffset with read_skill_resource if truncated.',
      parameters: { type: 'object', properties: { skillId }, required: ['skillId'], additionalProperties: false },
      schema: resourceOutput, render: result => result.content,
    },
    {
      name: 'read_skill_resource', description: 'Read a UTF-8 text file from a selected Skill package by relative path. This does not execute scripts.',
      parameters: {
        type: 'object', properties: {
          skillId, path: { type: 'string', minLength: 1, maxLength: 1024 },
          offset: { type: 'integer', minimum: 0, maximum: 1048576 },
          maxChars: { type: 'integer', minimum: 1, maximum: 64000 },
        }, required: ['skillId', 'path'], additionalProperties: false,
      },
      schema: resourceOutput, render: result => result.content,
    },
  ]
  for (const definition of definitions) {
    ctx.effect(() => ctx.platformResources.registerTool(definition.name, definition.name))
    ctx.tools.register({
      name: definition.name, description: definition.description, parameters: definition.parameters,
      output: { schema: definition.schema, render: (_args, result) => [{ type: 'text', text: definition.render(result) }] },
      isConcurrencySafe: () => definition.name !== 'execute_skills',
      async execute(args, execution) {
        const result = await ctx.platformResources.call(definition.name, args, { signal: execution.signal })
        if (definition.name === 'execute_skills') {
          if (!['succeeded', 'unknown', 'failed'].includes(result.status)) throw new Error('SKILL_EXECUTION_UNAVAILABLE')
        } else if (definition.name !== 'read_skill_artifact' && result.status !== 'ok') {
          throw new Error('SKILL_RESOURCE_UNAVAILABLE')
        }
        return result
      },
    })
  }
}
