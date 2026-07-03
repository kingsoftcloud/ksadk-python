export const appName = 'KsADK';

// GitHub Pages project sites are served under a sub-path (e.g. /ksadk-python).
// Set NEXT_PUBLIC_BASE_PATH at build time for production; empty in local dev.
export const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? '';

/** Prefix a /public asset with the deployment base path. */
export function assetPath(path: string): string {
  return `${basePath}${path}`;
}

export const docsRoute = '/docs';
export const docsImageRoute = '/og/docs';
export const docsContentRoute = '/llms.mdx/docs';

export const gitConfig = {
  user: 'kingsoftcloud',
  repo: 'ksadk-python',
  branch: 'main',
};
