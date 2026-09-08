import { readFile } from 'node:fs/promises';

export const name = 'studio-app';
export const inject = ['webServer'];

// Application composition only. All plugin services remain owned by Core.
export async function apply(ctx, config) {
  const html = await readFile(config.indexPath, 'utf8');
  const assets = html.match(/<script\b[^>]*type="module"[^>]*><\/script>|<link\b[^>]*rel="(?:stylesheet|modulepreload)"[^>]*>/g) || [];
  if (!assets.length) throw new Error('Studio frontend assets are missing; build Studio first');
  ctx.on('webserver/index-inject', table => table.push({
    kind: 'html', placement: 'head',
    html: '<script>window.__STUDIO_DSH_BOOT__=true</script>' + assets.join(''),
  }));
}
