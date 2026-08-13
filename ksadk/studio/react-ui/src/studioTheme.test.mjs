import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { transformWithOxc } from "vite";

const source = await readFile(new URL("./studioTheme.ts", import.meta.url), "utf8");
const transformed = await transformWithOxc(source, "studioTheme.ts", { lang: "ts" });
const moduleUrl = `data:text/javascript;base64,${Buffer.from(transformed.code).toString("base64")}`;
const theme = await import(moduleUrl);

test("normalizes persisted Studio theme preferences", () => {
  assert.equal(theme.normalizeThemePreference("system"), "system");
  assert.equal(theme.normalizeThemePreference("light"), "light");
  assert.equal(theme.normalizeThemePreference("dark"), "dark");
  assert.equal(theme.normalizeThemePreference("unknown"), "system");
  assert.equal(theme.normalizeThemePreference(null), "system");
});

test("resolves explicit and system Studio themes", () => {
  assert.equal(theme.resolveStudioTheme("light", true), "light");
  assert.equal(theme.resolveStudioTheme("dark", false), "dark");
  assert.equal(theme.resolveStudioTheme("system", false), "light");
  assert.equal(theme.resolveStudioTheme("system", true), "dark");
});
