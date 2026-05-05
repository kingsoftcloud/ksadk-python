import test from 'node:test';
import assert from 'node:assert/strict';

async function loadSessionListUtils() {
  return import('../src/utils/session-list.js').catch(() => null);
}

test('session list utils search sessions and pin active runs first', async () => {
  const sessionList = await loadSessionListUtils();

  assert.ok(sessionList, 'expected session list helpers to exist');
  const sessions = sessionList.normalizeSidebarSessions(
    [
      {
        SessionId: 'sess-old-running',
        Title: '部署卡住',
        ActiveRunStatus: 'in_progress',
        UpdatedAt: '2026-05-05T09:00:00Z',
      },
      {
        SessionId: 'sess-new-idle',
        Title: '模型配置',
        Summary: '确认上下文窗口',
        UpdatedAt: '2026-05-05T10:00:00Z',
      },
      {
        SessionId: 'sess-hidden',
        Title: '文件管理',
        UpdatedAt: '2026-05-05T11:00:00Z',
      },
    ],
    '配置',
  );

  assert.deepEqual(
    sessions.map((session) => session.SessionId),
    ['sess-new-idle'],
  );

  const sorted = sessionList.normalizeSidebarSessions(
    [
      {
        SessionId: 'sess-new-idle',
        Title: '模型配置',
        UpdatedAt: '2026-05-05T10:00:00Z',
      },
      {
        SessionId: 'sess-old-running',
        Title: '部署卡住',
        ActiveRunStatus: 'in_progress',
        UpdatedAt: '2026-05-05T09:00:00Z',
      },
    ],
    '',
  );

  assert.deepEqual(
    sorted.map((session) => session.SessionId),
    ['sess-old-running', 'sess-new-idle'],
  );
});

test('session list utils format model and context labels', async () => {
  const sessionList = await loadSessionListUtils();

  assert.ok(sessionList, 'expected session list helpers to exist');
  const session = {
    Model: { id: 'deepseek-v4-pro', display_name: 'DeepSeek V4 Pro' },
    ContextUsage: {
      percent: 37,
      used_tokens: 370000,
      context_window_tokens: 1000000,
    },
  };

  assert.equal(sessionList.formatSessionModelLabel(session), 'DeepSeek V4 Pro');
  assert.equal(sessionList.formatSessionContextLabel(session), '上下文 37%');
  assert.equal(sessionList.isSessionRunning({ ActiveRunStatus: 'completed' }), false);
  assert.equal(sessionList.isSessionRunning({ ActiveRunStatus: 'in_progress' }), true);
});
