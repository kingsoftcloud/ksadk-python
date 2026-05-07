import test from 'node:test';
import assert from 'node:assert/strict';

async function loadFeedbackUtils() {
  return import('../src/utils/feedback.js').catch(() => null);
}

async function loadModelOptionsUtils() {
  return import('../src/utils/model-options.js').catch(() => null);
}

test('feedback utils expose assistant response feedback state', async () => {
  const feedback = await loadFeedbackUtils();

  assert.ok(feedback, 'expected feedback helpers to exist');
  assert.equal(feedback.shouldRenderFeedbackControls({ role: 'user' }, false, false), false);
  assert.equal(
    feedback.shouldRenderFeedbackControls(
      { role: 'model', responseId: 'resp_123', eventId: 'ev_1' },
      true,
      true,
    ),
    false,
  );
  assert.equal(
    feedback.shouldRenderFeedbackControls(
      { role: 'model', responseId: 'resp_123', eventId: 'ev_1' },
      false,
      true,
    ),
    true,
  );

  assert.deepEqual(
    feedback.normalizeFeedback({
      AgentId: 'agent-1',
      SessionId: 'sess-1',
      ResponseId: 'resp_123',
      EventId: 'ev_1',
      Rating: 'down',
      Comment: '没有回答重点',
      TraceId: 'trace-1',
      RootSpanId: 'span-1',
      UpdatedAt: '2026-05-06T00:00:00Z',
    }),
    {
      agentId: 'agent-1',
      sessionId: 'sess-1',
      responseId: 'resp_123',
      eventId: 'ev_1',
      rating: 'down',
      comment: '没有回答重点',
      traceId: 'trace-1',
      rootSpanId: 'span-1',
      updatedAt: '2026-05-06T00:00:00Z',
    },
  );
});

test('feedback utils build action payloads for up and down feedback', async () => {
  const feedback = await loadFeedbackUtils();

  assert.ok(feedback, 'expected feedback helpers to exist');
  const message = {
    id: 'ev_1',
    role: 'model',
    responseId: 'resp_123',
    eventId: 'ev_1',
    traceId: 'trace-1',
    rootSpanId: 'span-1',
  };

  assert.deepEqual(
    feedback.buildUpsertFeedbackPayload({
      agentId: 'agent-1',
      sessionId: 'sess-1',
      message,
      rating: 'up',
    }),
    {
      AgentId: 'agent-1',
      SessionId: 'sess-1',
      ResponseId: 'resp_123',
      EventId: 'ev_1',
      Rating: 'up',
      Comment: '',
      TraceId: 'trace-1',
      RootSpanId: 'span-1',
    },
  );

  assert.deepEqual(
    feedback.buildUpsertFeedbackPayload({
      agentId: 'agent-1',
      sessionId: 'sess-1',
      message,
      rating: 'down',
      comment: '不准确',
    }),
    {
      AgentId: 'agent-1',
      SessionId: 'sess-1',
      ResponseId: 'resp_123',
      EventId: 'ev_1',
      Rating: 'down',
      Comment: '不准确',
      TraceId: 'trace-1',
      RootSpanId: 'span-1',
    },
  );

  assert.throws(
    () =>
      feedback.buildUpsertFeedbackPayload({
        agentId: 'agent-1',
        sessionId: 'sess-1',
        message: { id: 'ev_2', role: 'model' },
        rating: 'up',
      }),
    /missing response id/i,
  );
});

test('feedback utils support optimistic update and rollback', async () => {
  const feedback = await loadFeedbackUtils();

  assert.ok(feedback, 'expected feedback helpers to exist');
  const messages = [
    {
      id: 'ev_1',
      role: 'model',
      content: 'answer',
      responseId: 'resp_123',
      eventId: 'ev_1',
      feedback: { rating: 'up', comment: '' },
    },
  ];

  const { nextMessages, previousFeedback } = feedback.applyOptimisticFeedback(messages, {
    messageId: 'ev_1',
    rating: 'down',
    comment: '不完整',
  });

  assert.equal(nextMessages[0].feedback.rating, 'down');
  assert.equal(nextMessages[0].feedback.comment, '不完整');
  assert.deepEqual(previousFeedback, { rating: 'up', comment: '' });

  assert.deepEqual(
    feedback.rollbackFeedback(nextMessages, {
      messageId: 'ev_1',
      previousFeedback,
    }),
    messages,
  );
});

test('model options utils map thinking mode to request payload', async () => {
  const modelOptions = await loadModelOptionsUtils();

  assert.ok(modelOptions, 'expected model options helpers to exist');
  assert.equal(modelOptions.buildModelOptionsFromThinkingMode('auto'), undefined);
  assert.deepEqual(modelOptions.buildModelOptionsFromThinkingMode('disabled'), {
    thinking: { type: 'disabled' },
  });
  assert.deepEqual(modelOptions.buildModelOptionsFromThinkingMode('enabled'), {
    thinking: { type: 'enabled' },
  });
  assert.equal(modelOptions.normalizeThinkingMode('unexpected'), 'auto');
});
