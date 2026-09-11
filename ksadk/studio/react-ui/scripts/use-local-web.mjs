import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
const studio = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const web = resolve(studio, '../../../../ksadk-web');
function run(args, cwd) {
  const result = spawnSync('npm', args, { cwd, stdio: 'inherit', shell: false });
  if (result.status !== 0) process.exit(result.status || 1);
}
if (!existsSync(resolve(web, 'package.json'))) throw new Error('请将 ksadk-web 与 ksadk-python 放在同一目录后重试。');
run(['run', 'build:lib'], web);
const packed = spawnSync('npm', ['pack', '--ignore-scripts', '--json'], { cwd: web, encoding: 'utf8', shell: false });
if (packed.status !== 0) throw new Error(packed.stderr);
const artifact = JSON.parse(packed.stdout)[0].filename;
run(['install', '--no-save', '--package-lock=false', '--ignore-scripts', '--no-audit', '--no-fund', resolve(web, artifact)], studio);
