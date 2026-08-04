import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));

const applicationSources = [
  "src/app/core.js",
  "src/app/catalog-agents.js",
  "src/app/authoring.js",
  "src/app/agent-detail-build.js",
  "src/app/chat.js",
  "src/app/resources.js",
  "src/app/traces.js",
  "src/app/bootstrap.js",
];

const styleSources = [
  "src/styles/shell.css",
  "src/styles/tables.css",
  "src/styles/authoring-quick.css",
  "src/styles/authoring-wizard.css",
  "src/styles/authoring-modes.css",
  "src/styles/agents.css",
  "src/styles/chat.css",
  "src/styles/traces.css",
  "src/styles/build.css",
  "src/styles/overlays.css",
];

async function assemble(sources, destination) {
  const content = await Promise.all(
    sources.map((source) => readFile(resolve(root, source), "utf8")),
  );
  await writeFile(resolve(root, destination), content.join(""), "utf8");
}

await Promise.all([
  assemble(applicationSources, "../static/app.js"),
  assemble(styleSources, "../static/app.css"),
  assemble(["src/index.html"], "../static/index.html"),
]);
