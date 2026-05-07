import test from 'node:test';
import assert from 'node:assert/strict';

async function loadResponsesStreamUtils() {
  return import('../src/utils/responses-stream.js').catch(() => null);
}

test('responses stream utils normalize native tool done and tool output items', async () => {
  const responsesStream = await loadResponsesStreamUtils();

  assert.ok(responsesStream, 'expected Responses stream helpers to exist');
  const state = responsesStream.createResponsesStreamState();

  const toolAdded = responsesStream.normalizeResponsesStreamEvent({
    eventName: 'response.output_item.added',
    data: {
      item: {
        id: 'fc_1',
        call_id: 'call_1',
        type: 'function_call',
        name: 'wps_user_search',
        arguments: '',
      },
    },
    state,
  });
  assert.deepEqual(toolAdded, [
    {
      type: 'tool_upsert',
      name: 'wps_user_search',
      args: '',
      status: 'running',
    },
  ]);

  const argsDone = responsesStream.normalizeResponsesStreamEvent({
    eventName: 'response.output_item.done',
    data: {
      item: {
        id: 'fc_1',
        call_id: 'call_1',
        type: 'function_call',
        name: 'wps_user_search',
        arguments: '{"keyword":"夏雨"}',
      },
    },
    state,
  });
  assert.deepEqual(argsDone, [
    {
      type: 'tool_upsert',
      name: 'wps_user_search',
      args: '{"keyword":"夏雨"}',
      status: 'completed',
    },
  ]);

  const outputDone = responsesStream.normalizeResponsesStreamEvent({
    eventName: 'response.output_item.done',
    data: {
      item: {
        type: 'function_call_output',
        call_id: 'call_1',
        output: [{ id: 'u1', name: '夏雨' }],
      },
    },
    state,
  });
  assert.deepEqual(outputDone, [
    {
      type: 'tool_result',
      name: 'wps_user_search',
      output: '[\n  {\n    "id": "u1",\n    "name": "夏雨"\n  }\n]',
    },
  ]);
});

test('responses stream utils expose standard mcp approval request resume metadata', async () => {
  const responsesStream = await loadResponsesStreamUtils();

  assert.ok(responsesStream, 'expected Responses stream helpers to exist');
  const state = responsesStream.createResponsesStreamState();

  assert.deepEqual(
    responsesStream.normalizeResponsesStreamEvent({
      eventName: 'response.created',
      data: { id: 'resp_123' },
      state,
    }),
    [],
  );

  assert.deepEqual(
    responsesStream.normalizeResponsesStreamEvent({
      eventName: 'response.output_item.done',
      data: {
        item: {
          id: 'appr_123',
          type: 'mcp_approval_request',
          name: 'delete_file',
          server_label: 'workspace',
          arguments: '{"path":"README.md"}',
        },
      },
      state,
    }),
    [
      {
        type: 'tool_upsert',
        name: 'delete_file',
        args: '{"path":"README.md"}',
        status: 'paused',
        approvalRequestId: 'appr_123',
        previousResponseId: 'resp_123',
        serverLabel: 'workspace',
      },
    ],
  );
});

test('responses stream utils normalize reasoning summary and completed text variants', async () => {
  const responsesStream = await loadResponsesStreamUtils();

  assert.ok(responsesStream, 'expected Responses stream helpers to exist');
  const state = responsesStream.createResponsesStreamState();

  assert.deepEqual(
    responsesStream.normalizeResponsesStreamEvent({
      eventName: 'response.reasoning_summary_text.delta',
      data: { delta: '先查用户' },
      state,
    }),
    [{ type: 'reasoning_delta', text: '先查用户' }],
  );

  assert.deepEqual(
    responsesStream.normalizeResponsesStreamEvent({
      eventName: 'response.output_text.done',
      data: { text: '处理完成' },
      state,
    }),
    [{ type: 'text_final', text: '处理完成' }],
  );

  assert.deepEqual(
    responsesStream.normalizeResponsesStreamEvent({
      eventName: 'response.completed',
      data: {
        response: {
          output: [
            {
              type: 'message',
              content: [{ type: 'output_text', text: '最终答案' }],
            },
          ],
        },
      },
      state,
    }),
    [
      { type: 'text_final', text: '最终答案' },
      { type: 'terminal', status: 'completed' },
    ],
  );
});

test('responses stream utils do not use reasoning items as completed text', async () => {
  const responsesStream = await loadResponsesStreamUtils();

  assert.ok(responsesStream, 'expected Responses stream helpers to exist');
  const state = responsesStream.createResponsesStreamState();

  assert.deepEqual(
    responsesStream.normalizeResponsesStreamEvent({
      eventName: 'response.completed',
      data: {
        response: {
          output: [
            {
              id: 'rs_1',
              type: 'reasoning',
              summary: [{ text: '先分析问题' }],
            },
            {
              id: 'msg_1',
              type: 'message',
              content: [{ type: 'output_text', text: '最终答案' }],
            },
          ],
        },
      },
      state,
    }),
    [
      { type: 'reasoning_delta', text: '先分析问题' },
      { type: 'text_final', text: '最终答案' },
      { type: 'terminal', status: 'completed' },
    ],
  );
});

test('responses stream utils normalize tools and reasoning from completed response output', async () => {
  const responsesStream = await loadResponsesStreamUtils();

  assert.ok(responsesStream, 'expected Responses stream helpers to exist');
  const state = responsesStream.createResponsesStreamState();

  assert.deepEqual(
    responsesStream.normalizeResponsesStreamEvent({
      eventName: 'response.completed',
      data: {
        response: {
          id: 'resp_done',
          output: [
            {
              id: 'rs_1',
              type: 'reasoning',
              summary: [{ text: '需要先搜索资料' }],
            },
            {
              id: 'fc_1',
              call_id: 'call_1',
              type: 'function_call',
              name: 'web_search',
              arguments: '{"query":"agent tools"}',
            },
            {
              type: 'function_call_output',
              call_id: 'call_1',
              output: '{"result":"done"}',
            },
            {
              type: 'message',
              content: [{ type: 'output_text', text: '最终答案' }],
            },
          ],
        },
      },
      state,
    }),
    [
      { type: 'reasoning_delta', text: '需要先搜索资料' },
      {
        type: 'tool_upsert',
        name: 'web_search',
        args: '{"query":"agent tools"}',
        status: 'completed',
      },
      {
        type: 'tool_result',
        name: 'web_search',
        output: '{"result":"done"}',
      },
      { type: 'text_final', text: '最终答案' },
      { type: 'terminal', status: 'completed' },
    ],
  );
});

test('responses stream utils mark terminal response events', async () => {
  const responsesStream = await loadResponsesStreamUtils();

  assert.ok(responsesStream, 'expected Responses stream helpers to exist');
  const state = responsesStream.createResponsesStreamState();

  assert.deepEqual(
    responsesStream.normalizeResponsesStreamEvent({
      eventName: 'response.completed',
      data: { output_text: 'done' },
      state,
    }),
    [
      { type: 'text_final', text: 'done' },
      { type: 'terminal', status: 'completed' },
    ],
  );

  assert.deepEqual(
    responsesStream.normalizeResponsesStreamEvent({
      eventName: 'response.incomplete',
      data: {},
      state,
    }),
    [
      { type: 'incomplete' },
      { type: 'terminal', status: 'incomplete' },
    ],
  );
});
