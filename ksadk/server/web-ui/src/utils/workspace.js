export const WORKSPACE_ROOT_PATH = '.';

const MARKDOWN_EXTENSIONS = new Set(['.md', '.markdown', '.mdx']);
const PDF_EXTENSIONS = new Set(['.pdf']);
const IMAGE_EXTENSIONS = new Set([
  '.png',
  '.jpg',
  '.jpeg',
  '.gif',
  '.webp',
  '.svg',
  '.bmp',
  '.ico',
]);
const TEXT_EXTENSIONS = new Set([
  '.txt',
  '.log',
  '.json',
  '.yaml',
  '.yml',
  '.xml',
  '.html',
  '.css',
  '.js',
  '.jsx',
  '.ts',
  '.tsx',
  '.py',
  '.sh',
  '.sql',
  '.csv',
  '.tsv',
  '.toml',
  '.ini',
  '.env',
  '.lock',
  '.conf',
  '.c',
  '.cc',
  '.cpp',
  '.go',
  '.java',
  '.rs',
]);
const TEXT_MIME_TYPES = new Set([
  'application/json',
  'application/ld+json',
  'application/xml',
  'application/javascript',
  'application/x-javascript',
  'application/typescript',
  'application/x-sh',
  'application/x-yaml',
  'application/yaml',
  'text/csv',
  'text/tab-separated-values',
]);

export function normalizeWorkspacePath(path) {
  const value = String(path || '').trim();
  return value && value !== '/' ? value.replace(/^\/+|\/+$/g, '') || WORKSPACE_ROOT_PATH : WORKSPACE_ROOT_PATH;
}

export function isWorkspaceRootPath(path) {
  return normalizeWorkspacePath(path) === WORKSPACE_ROOT_PATH;
}

export function formatWorkspacePathLabel(path, rootLabel = 'Workspace') {
  const normalized = normalizeWorkspacePath(path);
  if (normalized === WORKSPACE_ROOT_PATH) {
    return rootLabel;
  }
  return `${rootLabel} / ${normalized.split('/').join(' / ')}`;
}

export function formatWorkspaceDirectoryPathLabel(path) {
  const normalized = normalizeWorkspacePath(path);
  if (normalized === WORKSPACE_ROOT_PATH) {
    return '/';
  }
  return `/${normalized}`;
}

export function buildWorkspaceBreadcrumbs(path, rootLabel = 'Workspace') {
  const normalized = normalizeWorkspacePath(path);
  if (normalized === WORKSPACE_ROOT_PATH) {
    return [{ label: rootLabel, path: WORKSPACE_ROOT_PATH }];
  }

  const segments = normalized.split('/').filter(Boolean);
  return [
    { label: rootLabel, path: WORKSPACE_ROOT_PATH },
    ...segments.map((segment, index) => ({
      label: segment,
      path: segments.slice(0, index + 1).join('/'),
    })),
  ];
}

function fileExtension(path) {
  const fileName = String(path || '').split('/').pop() || '';
  const dotIndex = fileName.lastIndexOf('.');
  return dotIndex >= 0 ? fileName.slice(dotIndex).toLowerCase() : '';
}

function normalizeMimeType(mimeType) {
  return String(mimeType || '')
    .split(';')[0]
    .trim()
    .toLowerCase();
}

export function resolveWorkspacePreviewKind({ path, mimeType }) {
  const ext = fileExtension(path);
  const normalizedMime = normalizeMimeType(mimeType);

  if (normalizedMime === 'text/markdown' || MARKDOWN_EXTENSIONS.has(ext)) {
    return 'markdown';
  }
  if (normalizedMime === 'application/pdf' || PDF_EXTENSIONS.has(ext)) {
    return 'pdf';
  }
  if (normalizedMime.startsWith('image/') || IMAGE_EXTENSIONS.has(ext)) {
    return 'image';
  }
  if (
    normalizedMime.startsWith('text/')
    || TEXT_MIME_TYPES.has(normalizedMime)
    || TEXT_EXTENSIONS.has(ext)
  ) {
    return 'text';
  }
  return 'unsupported';
}

export function resolveWorkspacePanelPresentation({ isMobile }) {
  if (isMobile) {
    return {
      renderMode: 'sheet',
      modal: true,
      showOverlay: true,
      preventOutsideClose: false,
      side: 'bottom',
    };
  }
  return {
    renderMode: 'inline',
    modal: false,
    showOverlay: false,
    preventOutsideClose: true,
    side: 'right',
  };
}
