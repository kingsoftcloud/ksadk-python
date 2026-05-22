export const WORKSPACE_ROOT_PATH = '.';
export const WORKSPACE_FILES_BASE_PATH = '/_ksadk/workspace/v1/files';

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
  const value = String(path || '').trim().replace(/\\/g, '/');
  if (!value || value === '/') {
    return WORKSPACE_ROOT_PATH;
  }

  const segments = [];
  for (const rawSegment of value.split('/')) {
    const segment = rawSegment.trim();
    if (!segment || segment === '.') {
      continue;
    }
    if (segment === '..') {
      if (segments.length > 0) {
        segments.pop();
      }
      continue;
    }
    segments.push(segment);
  }

  return segments.length > 0 ? segments.join('/') : WORKSPACE_ROOT_PATH;
}

function encodeWorkspacePath(path) {
  const normalized = normalizeWorkspacePath(path);
  if (normalized === WORKSPACE_ROOT_PATH) {
    return '';
  }
  return normalized
    .split('/')
    .filter(Boolean)
    .map((segment) => encodeURIComponent(segment))
    .join('/');
}

export function buildWorkspaceFileUrl(path) {
  const encodedPath = encodeWorkspacePath(path);
  return encodedPath ? `${WORKSPACE_FILES_BASE_PATH}/${encodedPath}` : WORKSPACE_FILES_BASE_PATH;
}

export function buildWorkspaceFileBaseUrl(path) {
  const normalized = normalizeWorkspacePath(path);
  if (normalized === WORKSPACE_ROOT_PATH || !normalized.includes('/')) {
    return `${WORKSPACE_FILES_BASE_PATH}/`;
  }
  const dirPath = normalized.substring(0, normalized.lastIndexOf('/'));
  const encodedDirPath = encodeWorkspacePath(dirPath);
  return encodedDirPath
    ? `${WORKSPACE_FILES_BASE_PATH}/${encodedDirPath}/`
    : `${WORKSPACE_FILES_BASE_PATH}/`;
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

const HTML_EXTENSIONS = new Set(['.html', '.htm']);

export function resolveWorkspacePreviewKind({ path, mimeType }) {
  const ext = fileExtension(path);
  const normalizedMime = normalizeMimeType(mimeType);

  if (normalizedMime === 'text/html' || HTML_EXTENSIONS.has(ext)) {
    return 'html';
  }
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

const PLAIN_TEXT_EXTENSIONS = new Set([
  '.txt',
  '.log',
  '.csv',
  '.tsv',
  '.env',
  '.ini',
  '.conf',
  '.lock',
]);

export function resolveWorkspaceEditKind({ path, mimeType }) {
  const ext = fileExtension(path);
  const normalizedMime = normalizeMimeType(mimeType);

  if (HTML_EXTENSIONS.has(ext) || normalizedMime === 'text/html') {
    return 'html';
  }
  if (normalizedMime === 'text/markdown' || MARKDOWN_EXTENSIONS.has(ext)) {
    return 'markdown';
  }
  if (normalizedMime === 'application/pdf' || PDF_EXTENSIONS.has(ext)) {
    return null;
  }
  if (normalizedMime.startsWith('image/') || IMAGE_EXTENSIONS.has(ext)) {
    return null;
  }
  if (PLAIN_TEXT_EXTENSIONS.has(ext) || normalizedMime === 'text/plain') {
    return 'text';
  }
  if (
    normalizedMime.startsWith('text/')
    || TEXT_MIME_TYPES.has(normalizedMime)
    || TEXT_EXTENSIONS.has(ext)
  ) {
    return 'code';
  }
  return null;
}

/**
 * @param {{ isMobile: boolean }} options
 * @returns {{
 *   renderMode: 'sheet' | 'inline';
 *   modal: boolean;
 *   showOverlay: boolean;
 *   preventOutsideClose: boolean;
 *   side: 'top' | 'bottom' | 'left' | 'right';
 * }}
 */
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

export function canAccessWorkspaceFiles({ workspaceFiles, accessMode }) {
  if (!workspaceFiles?.Enabled) {
    return false;
  }
  const mode = String(accessMode || '').trim().toLowerCase();
  return mode === 'owner' || mode === 'private';
}
