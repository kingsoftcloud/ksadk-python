import { cp, mkdir, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const webUiRoot = path.resolve(__dirname, '..');
const distDir = path.join(webUiRoot, 'dist');
const staticDir = path.resolve(webUiRoot, '../static');
const staticAssetsDir = path.join(staticDir, 'assets');

await rm(staticDir, { recursive: true, force: true });
await mkdir(staticDir, { recursive: true });
await cp(path.join(distDir, 'assets'), staticAssetsDir, { recursive: true, force: true });

for (const fileName of ['index.html', 'favicon.svg', 'icons.svg']) {
  await cp(path.join(distDir, fileName), path.join(staticDir, fileName), { force: true });
}
