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
    { now: Date.parse('2026-05-05T09:01:00Z') },
  );

  assert.deepEqual(
    sorted.map((session) => session.SessionId),
    ['sess-old-running', 'sess-new-idle'],
  );
});

test('session list utils do not pin stale in-progress sessions', async () => {
  const sessionList = await loadSessionListUtils();

  assert.ok(sessionList, 'expected session list helpers to exist');
  const now = Date.parse('2026-05-27T00:20:00Z');
  const sorted = sessionList.normalizeSidebarSessions(
    [
      {
        SessionId: 'sess-stale-running',
        Title: '旧运行',
        ActiveRunStatus: 'in_progress',
        UpdatedAt: '2026-05-27T00:10:00Z',
      },
      {
        SessionId: 'sess-recent-idle',
        Title: '新会话',
        ActiveRunStatus: 'completed',
        UpdatedAt: '2026-05-27T00:19:00Z',
      },
    ],
    '',
    { now, activeStaleAfterMs: 5 * 60 * 1000 },
  );

  assert.deepEqual(
    sorted.map((session) => session.SessionId),
    ['sess-recent-idle', 'sess-stale-running'],
  );
  assert.equal(
    sessionList.resolveCompactSessionMeta(sorted[1], { now, activeStaleAfterMs: 5 * 60 * 1000 }).running,
    false,
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

test('session list utils expose compact sidebar status labels', async () => {
  const sessionList = await loadSessionListUtils();

  assert.ok(sessionList, 'expected session list helpers to exist');
  const now = Date.parse('2026-05-07T08:46:00Z');
  assert.deepEqual(
    sessionList.resolveCompactSessionMeta({
      ActiveRunStatus: 'in_progress',
      UpdatedAt: '2026-05-07T08:45:00Z',
    }, { now }),
    {
      running: true,
      label: '',
    },
  );
  assert.deepEqual(
    sessionList.resolveCompactSessionMeta({
      ActiveRunStatus: 'completed',
      UpdatedAt: '2026-05-07T08:45:00Z',
    }),
    {
      running: false,
      label: '5月7日 16:45',
    },
  );
});
