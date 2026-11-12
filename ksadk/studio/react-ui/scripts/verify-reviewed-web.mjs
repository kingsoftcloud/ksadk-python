import { createHash } from "node:crypto";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const studio = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));
const pinned = readJson(join(studio, "vendor/ksadk-web-source.json"));
const local = process.env.KSADK_WEB_TARBALL;
// npm runs lifecycle scripts in the package directory; relative overrides are
// supplied from the caller's directory, including `npm --prefix ... run build`.
const localPath = local && resolve(process.env.INIT_CWD || process.cwd(), local);
const manifest = local ? JSON.parse(execFileSync(process.env.PYTHON || "python3", [
  resolve(studio, "../../../scripts/reviewed_web_assets.py"), "identity", "--artifact", localPath,
], { encoding: "utf8" })) : pinned;
const dependency = readJson(join(studio, "package.json")).dependencies[manifest.package_name];
const expectedDependency = pinned.registry_release ? pinned.package_version : `file:vendor/${pinned.artifact}`;
if (dependency !== expectedDependency) {
  throw new Error("Studio shared UI dependency differs from its reviewed source manifest.");
}
if (!local && !manifest.registry_release) {
  const artifact = readFileSync(join(studio, "vendor", manifest.artifact));
  if (createHash("sha256").update(artifact).digest("hex") !== manifest.artifact_sha256) {
    throw new Error("Reviewed shared UI artifact checksum changed.");
  }
}
const installed = join(studio, "node_modules", manifest.package_name);
if (!existsSync(join(installed, "package.json"))) {
  throw new Error("Reviewed shared UI is not installed. Run npm ci before building Studio.");
}
const packageJson = readJson(join(installed, "package.json"));
const sourceFile = manifest.registry_release ? "RELEASE_PROVENANCE.json" : "SOURCE_CANDIDATE.json";
const source = existsSync(join(installed, sourceFile)) ? readJson(join(installed, sourceFile)) : {};
if (packageJson.name !== manifest.package_name || packageJson.version !== manifest.package_version
  || (!local && source.source_commit !== manifest.source_commit)) {
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
