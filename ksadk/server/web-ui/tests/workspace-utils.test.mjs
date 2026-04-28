import test from 'node:test';
import assert from 'node:assert/strict';

async function loadWorkspaceUtils() {
  return import('../src/utils/workspace.js').catch(() => null);
}

test('workspace path helpers hide the root sentinel and build readable breadcrumbs', async () => {
  const workspaceUtils = await loadWorkspaceUtils();

  assert.ok(workspaceUtils, 'expected workspace helpers to exist');
  assert.equal(workspaceUtils.isWorkspaceRootPath('.'), true);
  assert.equal(workspaceUtils.formatWorkspacePathLabel('.', 'Workspace'), 'Workspace');
  assert.equal(workspaceUtils.formatWorkspacePathLabel('docs/specs', 'Workspace'), 'Workspace / docs / specs');
  assert.equal(workspaceUtils.formatWorkspaceDirectoryPathLabel('.'), '/');
  assert.equal(
    workspaceUtils.formatWorkspaceDirectoryPathLabel('docs/specs'),
    '/docs/specs',
  );
  assert.deepEqual(
    workspaceUtils.buildWorkspaceBreadcrumbs('docs/specs', 'Workspace'),
    [
      { label: 'Workspace', path: '.' },
      { label: 'docs', path: 'docs' },
      { label: 'specs', path: 'docs/specs' },
    ],
  );
});

test('workspace preview helpers classify supported preview types', async () => {
  const workspaceUtils = await loadWorkspaceUtils();

  assert.ok(workspaceUtils, 'expected workspace helpers to exist');
  assert.equal(
    workspaceUtils.resolveWorkspacePreviewKind({ path: 'README.md', mimeType: 'text/markdown' }),
    'markdown',
  );
  assert.equal(
    workspaceUtils.resolveWorkspacePreviewKind({ path: 'config.json', mimeType: 'application/json' }),
    'text',
  );
  assert.equal(
    workspaceUtils.resolveWorkspacePreviewKind({ path: 'diagram.png', mimeType: 'image/png' }),
    'image',
  );
  assert.equal(
    workspaceUtils.resolveWorkspacePreviewKind({ path: 'report.pdf', mimeType: 'application/pdf' }),
    'pdf',
  );
  assert.equal(
    workspaceUtils.resolveWorkspacePreviewKind({ path: 'artifact.pdf', mimeType: 'application/octet-stream' }),
    'pdf',
  );
  assert.equal(
    workspaceUtils.resolveWorkspacePreviewKind({
      path: 'proposal.docx',
      mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    }),
    'unsupported',
  );
});

test('workspace panel presentation keeps desktop chat interactive', async () => {
  const workspaceUtils = await loadWorkspaceUtils();

  assert.ok(workspaceUtils, 'expected workspace helpers to exist');
  assert.deepEqual(
    workspaceUtils.resolveWorkspacePanelPresentation({ isMobile: false }),
    {
      renderMode: 'inline',
      modal: false,
      showOverlay: false,
      preventOutsideClose: true,
      side: 'right',
    },
  );
  assert.deepEqual(
    workspaceUtils.resolveWorkspacePanelPresentation({ isMobile: true }),
    {
      renderMode: 'sheet',
      modal: true,
      showOverlay: true,
      preventOutsideClose: false,
      side: 'bottom',
    },
  );
});

test('workspace access allows owner and private links but blocks share links', async () => {
  const workspaceUtils = await loadWorkspaceUtils();

  assert.ok(workspaceUtils, 'expected workspace helpers to exist');
  const enabledCapability = { Enabled: true };
  assert.equal(
    workspaceUtils.canAccessWorkspaceFiles({
      workspaceFiles: enabledCapability,
      accessMode: 'Owner',
    }),
    true,
  );
  assert.equal(
    workspaceUtils.canAccessWorkspaceFiles({
      workspaceFiles: enabledCapability,
      accessMode: 'Private',
    }),
    true,
  );
  assert.equal(
    workspaceUtils.canAccessWorkspaceFiles({
      workspaceFiles: enabledCapability,
      accessMode: 'Share',
    }),
    false,
  );
  assert.equal(
    workspaceUtils.canAccessWorkspaceFiles({
      workspaceFiles: { Enabled: false },
      accessMode: 'Private',
    }),
    false,
  );
});
