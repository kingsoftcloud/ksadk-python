import assert from "node:assert/strict"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { build, loadConfigFromFile } from "vite"

test("production build preserves whitespace inside JavaScript literals", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "studio-build-literals-"))
  try {
    // Lit uses this whitespace class when parsing bound HTML attributes.
    // Trimming generated JS as plain text corrupts the runtime parser.
    const whitespace = "[ \t\n\f\r]"
    const entry = path.join(root, "entry.js")
    await writeFile(entry, `export const whitespace = ${JSON.stringify(whitespace)};`)
    const configPath = fileURLToPath(new URL("../vite.config.ts", import.meta.url))
    const loaded = await loadConfigFromFile({ command: "build", mode: "production" }, configPath)
    assert.ok(loaded)
    const result = await build({
      ...loaded.config,
      configFile: false,
      root,
      logLevel: "silent",
      build: {
        ...loaded.config.build,
        outDir: path.join(root, "dist"),
        write: false,
        minify: true,
        modulePreload: false,
        rollupOptions: {
          input: entry,
          preserveEntrySignatures: "strict",
          output: { format: "es" },
        },
      },
    })
    const bundles = Array.isArray(result) ? result : [result]
    const chunk = bundles.flatMap(bundle => bundle.output).find(output => output.type === "chunk")
    assert.ok(chunk)
    const module = await import(`data:text/javascript,${encodeURIComponent(chunk.code)}`)
    assert.equal(module.whitespace, whitespace)
    assert.ok(new RegExp(module.whitespace).test(" "))
    assert.ok(new RegExp(module.whitespace).test("\t"))
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
