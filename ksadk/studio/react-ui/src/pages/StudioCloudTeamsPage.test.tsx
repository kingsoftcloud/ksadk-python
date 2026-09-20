import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { TEAMS_API_VERSION, decodeTeamWorkspaceSnapshot } from '@kingsoftcloud/ksadk-web/teams';
import { StudioCloudTeamsPage } from './StudioCloudTeamsPage';
import { type TeamsLifecycle } from './TeamsAvailability';
import { teamSnapshot } from '../test/teamsFixtures';

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock('../api', () => ({ apiFetch: fetchMock }));
const local = teamSnapshot();
const scope = { authorityId: local.group.authorityRef, ownerScopeRef: 'verified-owner', groupId: local.group.groupId };
const lifecycle: TeamsLifecycle = { enabled: true, mode: 'server', health: 'degraded', authorityRef: scope.authorityId, authorityId: scope.authorityId, ownerScopeRef: scope.ownerScopeRef, apiVersion: TEAMS_API_VERSION, features: ['workspace-projection.v1'] };
const group = { ...local.group, policy: { taskAcceptance: 'human', peerWake: false } };
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });

beforeEach(() => {
  window.history.replaceState(null, '', '#/workspace/teams?groupId=fixture-group');
  fetchMock.mockReset().mockImplementation(async (url: string, init: RequestInit = {}) => {
    if (url.includes('/events?')) return new Response(new ReadableStream({ start(controller) { init.signal?.addEventListener('abort', () => controller.close(), { once: true }); } }), { headers: { 'Content-Type': 'text/event-stream' } });
    if (url.includes('/workspace?')) return json(decodeTeamWorkspaceSnapshot({ apiVersion: TEAMS_API_VERSION, viewVersion: 'workspace/v1', scope, group, snapshotId: 'real-snapshot-contract', watermark: 0,
      members: local.members.map(member => ({ ...member, responsibility: member.responsibility || '', activeRunId: null, reason: null, binding: { ...member.binding, availability: { state: 'unavailable', code: 'node_offline', reason: '本地节点离线', action: null } } })),
      runSummaries: [], selectedRun: null, selectedRunMembers: [], taskSummaries: [], pendingInteractions: [], recentMessages: [], artifactSummaries: [],
      cursors: { runSummaries: null, taskSummaries: null, pendingInteractions: null, recentMessages: null, artifactSummaries: null } }));
    return json({ apiVersion: TEAMS_API_VERSION, scope: { authorityId: scope.authorityId, ownerScopeRef: scope.ownerScopeRef }, items: [{ ...group, memberCount: 2, pendingCount: 0, unreadCount: 0, lastMessage: '已有历史' }], nextCursor: null });
  });
});

it('keeps scoped cloud history readable while the local node is offline and writes are degraded', async () => {
  render(<StudioCloudTeamsPage lifecycle={lifecycle} onRetry={() => {}} />);
  expect(await screen.findByRole('heading', { name: group.name })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '新任务' })).toBeDisabled();
  expect(screen.getByRole('textbox', { name: '团队协作目标' })).toBeEnabled();
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/events?'))).toBe(true));
  for (const [, init] of fetchMock.mock.calls) expect(new Headers(init.headers).get('X-Teams-Owner-Scope-Ref')).toBe(scope.ownerScopeRef);
  expect(fetchMock.mock.calls.every(([, init]) => !init.method || init.method === 'GET')).toBe(true);
});

it('rejects a directory reply from a changed identity before showing another owner group', async () => {
  window.history.replaceState(null, '', '#/workspace/teams');
  fetchMock.mockResolvedValue(json({ apiVersion: TEAMS_API_VERSION, scope: { authorityId: scope.authorityId, ownerScopeRef: 'another-owner' }, items: [{ ...group, memberCount: 2, pendingCount: 0, unreadCount: 0, lastMessage: 'foreign' }], nextCursor: null }));
  render(<StudioCloudTeamsPage lifecycle={lifecycle} onRetry={() => {}} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('其他身份');
  expect(screen.queryByText(group.name)).not.toBeInTheDocument();
});
