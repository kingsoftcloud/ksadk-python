import { readFileSync } from 'node:fs';

export const name = 'studio-app';
export const inject = ['webServer'];

// Application composition only. All plugin services remain owned by Core.
export async function apply(ctx, config) {
  const assetTags = () => {
    const html = readFileSync(config.indexPath, 'utf8');
    const assets = html.match(/<script\b[^>]*type="module"[^>]*><\/script>|<link\b[^>]*rel="(?:stylesheet|modulepreload)"[^>]*>/g) || [];
    if (!assets.length) throw new Error('Studio frontend assets are missing; build Studio first');
    return assets.join('');
  };
  assetTags(); // Validate at boot, but do not retain filenames across frontend rebuilds.
  ctx.on('webserver/index-inject', table => table.push({
    kind: 'html', placement: 'head',
    html: '<script>window.__STUDIO_DSH_BOOT__=true</script>' + assetTags(),
  }));
}
