import { createHash } from "node:crypto";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const studio = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));
const manifest = readJson(join(studio, "vendor/ksadk-web-source.json"));
const dependency = readJson(join(studio, "package.json")).dependencies[manifest.package_name];
if (dependency !== `file:vendor/${manifest.artifact}`) {
  throw new Error("Studio shared UI dependency differs from its reviewed source manifest.");
}
const artifact = readFileSync(join(studio, "vendor", manifest.artifact));
if (createHash("sha256").update(artifact).digest("hex") !== manifest.artifact_sha256) {
  throw new Error("Reviewed shared UI artifact checksum changed.");
}
const installed = join(studio, "node_modules", manifest.package_name);
if (!existsSync(join(installed, "SOURCE_CANDIDATE.json"))) {
  throw new Error("Reviewed shared UI is not installed. Run npm ci before building Studio.");
}
const source = readJson(join(installed, "SOURCE_CANDIDATE.json"));
if (source.source_commit !== manifest.source_commit) {
  throw new Error("Installed shared UI is from another source commit. Run npm ci.");
}
function files(root) {
  return readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const path = join(root, entry.name);
    if (entry.isSymbolicLink()) throw new Error("Shared UI distribution must not contain symlinks.");
    return entry.isDirectory() ? files(path) : [path];
  });
}
for (const [directory, expected] of [
  ["dist-lib", manifest.library_tree_sha256],
  ["dist-ksadk", manifest.hosted_tree_sha256],
]) {
  const root = join(installed, directory);
  const digest = createHash("sha256");
  for (const path of files(root).sort()) {
    const hash = createHash("sha256").update(readFileSync(path)).digest("hex");
    digest.update(`${relative(root, path).split("\\").join("/")}\0${hash}\n`);
  }
  if (digest.digest("hex") !== expected) {
    throw new Error(`Installed ${directory} differs from the reviewed distribution. Run npm ci.`);
  }
}
console.log(`Verified Studio shared UI source ${manifest.source_commit}.`);
