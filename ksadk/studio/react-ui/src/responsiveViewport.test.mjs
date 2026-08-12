import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { transformWithOxc } from "vite";

const source = await readFile(new URL("./responsiveViewport.ts", import.meta.url), "utf8");
const transformed = await transformWithOxc(source, "responsiveViewport.ts", { lang: "ts" });
const moduleUrl = `data:text/javascript;base64,${Buffer.from(transformed.code).toString("base64")}`;
const responsive = await import(moduleUrl);

test("maps every supported breakpoint boundary to one viewport mode", () => {
  assert.equal(responsive.viewportModeForWidth(768), "compact");
  assert.equal(responsive.viewportModeForWidth(1023), "compact");
  assert.equal(responsive.viewportModeForWidth(1024), "laptop");
  assert.equal(responsive.viewportModeForWidth(1439), "laptop");
  assert.equal(responsive.viewportModeForWidth(1440), "desktop");
  assert.equal(responsive.viewportModeForWidth(1919), "desktop");
  assert.equal(responsive.viewportModeForWidth(1920), "wide");
  assert.equal(responsive.viewportModeForWidth(3840), "wide");
});
