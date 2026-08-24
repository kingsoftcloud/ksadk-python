import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const [entry, foundation, tokens, finalLayer] = await Promise.all([
  readFile(new URL("./main.tsx", import.meta.url), "utf8"),
  readFile(new URL("./soft-block.css", import.meta.url), "utf8"),
  readFile(new URL("./studio.css", import.meta.url), "utf8"),
  readFile(new URL("./kingdesign.css", import.meta.url), "utf8"),
]);

test("loads the company design layer after the legacy Studio styles", () => {
  assert.match(entry, /import "\.\/index\.css";\s*import "\.\/kingdesign\.css";/);
});

test("does not globally erase component borders or overlay elevation", () => {
  assert.doesNotMatch(foundation, /\*\s*,\s*\*::before\s*,\s*\*::after\s*\{\s*border-width:\s*0;/s);
  assert.match(finalLayer, /border:\s*1px solid var\(--border-card\)/);
  assert.match(finalLayer, /box-shadow:\s*var\(--shadow-overlay\) !important/);
});

test("keeps browser zoom, AI message states, and scrollbars in the shared contract", () => {
  assert.match(finalLayer, /\.app-shell\s*\{\s*--studio-app-rail:\s*216px;\s*min-width:\s*0;/s);
  assert.match(finalLayer, /\.studio-data-table-scroll\s*\{\s*overflow:\s*auto;/s);
  assert.match(finalLayer, /\.runtime-resource-group\s*\{\s*padding:\s*20px;\s*border:\s*1px solid var\(--border-card\);/s);
  assert.match(finalLayer, /#pageHeaderActions > \.tag\s*\{\s*display:\s*none;/s);
  assert.match(finalLayer, /\.chat-composer\s*\{[\s\S]*?border:\s*1px solid var\(--kc-composer-border\)/s);
  assert.match(finalLayer, /::-webkit-scrollbar-thumb/);
});

test("derives interactive AI surfaces from tokens in both light and dark themes", () => {
  assert.match(finalLayer, /:root:not\(\.dark\)\s*\{/);
  assert.match(finalLayer, /:root\.dark\s*\{/);
  assert.match(finalLayer, /background:\s*var\(--kc-user-bubble\)/);
  assert.match(finalLayer, /background-color:\s*var\(--kc-graph-fill\)/);
});

test("uses soft borders for the creation workbench and blue only for selection", () => {
  // The phase branch retains the current Studio token palette; this assertion
  // deliberately protects the semantic card-border contract rather than an
  // obsolete literal from the reference branch.
  assert.match(tokens, /--border-card:\s*#[0-9a-f]{6};/i);
  assert.match(tokens, /--border-strong:\s*#[0-9a-f]{6};/i);
  assert.match(finalLayer, /\.create-shell \.template-card\s*\{[\s\S]*?border:\s*1px solid var\(--border\)/);
  assert.match(finalLayer, /\.create-shell \.template-card\.selected\s*\{[\s\S]*?border-color:\s*var\(--kc-accent-border\)/);
  assert.match(finalLayer, /\.create-shell \.authoring-mode-tabs button\.active,[\s\S]*?border-color:\s*var\(--kc-accent-border\)/);
  assert.match(finalLayer, /\.create-shell \.wizard-step \.step-number\s*\{[\s\S]*?border:\s*1px solid var\(--border-strong\)/);
  assert.match(finalLayer, /\.global-header \.crumb\s*\{[\s\S]*?border:\s*1px solid var\(--border\)/);
});

test("resets browser button chrome and gives shared selection controls soft borders", () => {
  assert.match(finalLayer, /button\s*\{[\s\S]*?appearance:\s*none;[\s\S]*?border:\s*0;/);
  assert.match(finalLayer, /\.page-tabs button,[\s\S]*?\.segmented-control button\s*\{[\s\S]*?border:\s*1px solid transparent;/);
  assert.match(finalLayer, /\.page-tabs button\[aria-selected="true"\],[\s\S]*?border-color:\s*var\(--kc-accent-border\)/);
  assert.match(finalLayer, /\.choice-card,[\s\S]*?\.suggestion-list button\s*\{[\s\S]*?border:\s*1px solid var\(--border\)/);
  assert.match(finalLayer, /\.chat-session-main\s*\{[\s\S]*?border:\s*1px solid transparent;/);
});

test("keeps Agent editor icons and shared form grids geometrically aligned", () => {
  assert.match(finalLayer, /\.studio-field-label-row\s*\{[\s\S]*?min-height:\s*24px;[\s\S]*?align-items:\s*center;/);
  assert.match(finalLayer, /\.quick-runtime-strip \.runtime-logo,[\s\S]*?display:\s*inline-grid;[\s\S]*?line-height:\s*0;/);
  assert.match(finalLayer, /\.runtime-logo > svg,[\s\S]*?display:\s*block;[\s\S]*?margin:\s*auto;/);
  assert.match(finalLayer, /\.agent-edit-nav button\.active\s*\{[\s\S]*?border-color:\s*var\(--kc-accent-border\)/);
});

test("keeps cloud versions in a bounded compact grid instead of native radio geometry", () => {
  assert.match(foundation, /\.deployment-version-list\s*\{[\s\S]*?max-height:\s*430px;[\s\S]*?overflow-x:\s*hidden;[\s\S]*?overflow-y:\s*auto;/);
  assert.match(foundation, /\.deployment-version-option\s*\{[\s\S]*?display:\s*grid;[\s\S]*?width:\s*100%;[\s\S]*?min-width:\s*0;[\s\S]*?grid-template-columns:/);
  assert.match(foundation, /\.deployment-version-name\s*\{[\s\S]*?overflow:\s*hidden;[\s\S]*?text-overflow:\s*ellipsis;/);
  assert.doesNotMatch(foundation, /\.deployment-version-option\s*>\s*input/);
});
