import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const [entry, foundation, tokens, responsive, finalLayer, resourcesPage] = await Promise.all([
  readFile(new URL("./main.tsx", import.meta.url), "utf8"),
  readFile(new URL("./soft-block.css", import.meta.url), "utf8"),
  readFile(new URL("./studio.css", import.meta.url), "utf8"),
  readFile(new URL("./responsive.css", import.meta.url), "utf8"),
  readFile(new URL("./kingdesign.css", import.meta.url), "utf8"),
  readFile(new URL("./pages/ResourcesPage.tsx", import.meta.url), "utf8"),
]);

test("loads the company design layer after the legacy Studio styles", () => {
  assert.match(entry, /import "\.\/index\.css";\s*import "\.\/kingdesign\.css";/);
});

test("uses one wide content grid for document, workbench, and data pages", () => {
  assert.match(responsive, /--studio-page-max:\s*1760px;/);
  assert.match(responsive, /\.app-shell \.page-container\[data-layout="document"\]\s*\{\s*max-width:\s*var\(--studio-page-max\);/s);
  assert.match(responsive, /\.app-shell \.page-container\[data-layout="workbench"\]\s*\{\s*max-width:\s*var\(--studio-page-max\);/s);
  assert.match(responsive, /\.app-shell \.page-container\[data-layout="data"\]\s*\{\s*max-width:\s*var\(--studio-page-max\);/s);
  assert.match(foundation, /\.delivery-page\s*\{[\s\S]*?max-width:\s*var\(--studio-page-max, 1760px\);/);
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

test("keeps the resource catalogue inside the bounded data-page scroll contract", () => {
  assert.match(resourcesPage, /className="page-container resources-page" data-layout="data" data-scroll-mode="data"/);
  assert.match(responsive, /\.app-shell \.page-container\[data-layout="data"\]\[data-scroll-mode="data"\]\s*\{[\s\S]*?height:\s*calc\(100dvh - 64px\);[\s\S]*?overflow:\s*hidden;/);
  assert.match(responsive, /\.app-shell \.table-data-body \.data-scroll-region\s*\{[\s\S]*?overflow:\s*auto;[\s\S]*?overscroll-behavior:\s*contain;/);
});

test("derives conversation surfaces from tokens in both light and dark themes", () => {
  assert.match(finalLayer, /:root:not\(\.dark\)\s*\{/);
  assert.match(finalLayer, /:root\.dark\s*\{/);
  assert.match(finalLayer, /background:\s*var\(--kc-user-bubble\)/);
});

test("uses soft borders for the creation workbench and blue only for selection", () => {
  // The phase branch retains the current Studio token palette; this assertion
  // deliberately protects the semantic card-border contract rather than an
  // obsolete literal from the reference branch.
  assert.match(tokens, /--border-card:\s*#[0-9a-f]{6};/i);
  assert.match(tokens, /--border-strong:\s*#[0-9a-f]{6};/i);
  assert.match(finalLayer, /\.create-shell \.template-card\s*\{[\s\S]*?border:\s*1px solid var\(--studio-border\)/);
  assert.match(finalLayer, /\.create-shell \.template-card\.selected\s*\{[\s\S]*?border-color:\s*var\(--kc-accent-border\)/);
  assert.match(finalLayer, /\.create-shell \.authoring-mode-tabs button\.active,[\s\S]*?border-color:\s*var\(--kc-accent-border\)/);
  assert.match(finalLayer, /\.create-shell \.wizard-step \.step-number\s*\{[\s\S]*?border:\s*1px solid var\(--border-strong\)/);
  assert.match(finalLayer, /\.global-header \.crumb\s*\{[\s\S]*?border:\s*1px solid var\(--studio-border\)/);
});

test("resets browser button chrome and gives shared selection controls soft borders", () => {
  assert.match(finalLayer, /button\s*\{[\s\S]*?appearance:\s*none;[\s\S]*?border:\s*0;/);
  assert.match(finalLayer, /\.page-tabs button,[\s\S]*?\.segmented-control button\s*\{[\s\S]*?border:\s*1px solid transparent;/);
  assert.match(finalLayer, /\.page-tabs button\[aria-selected="true"\],[\s\S]*?border-color:\s*var\(--kc-accent-border\)/);
  assert.match(finalLayer, /\.choice-card,[\s\S]*?\.suggestion-list button\s*\{[\s\S]*?border:\s*1px solid var\(--studio-border\)/);
  assert.match(finalLayer, /\.chat-session-main\s*\{[\s\S]*?border:\s*1px solid transparent;/);
});

test("keeps Agent editor icons and shared form grids geometrically aligned", () => {
  assert.match(finalLayer, /\.studio-field-label-row\s*\{[\s\S]*?min-height:\s*24px;[\s\S]*?align-items:\s*center;/);
  assert.match(finalLayer, /\.quick-runtime-strip \.runtime-logo,[\s\S]*?display:\s*inline-grid;[\s\S]*?line-height:\s*0;/);
  assert.match(finalLayer, /\.runtime-logo > svg,[\s\S]*?display:\s*block;[\s\S]*?margin:\s*auto;/);
  assert.match(finalLayer, /\.agent-edit-nav button\.active\s*\{[\s\S]*?border-color:\s*var\(--kc-accent-border\)/);
});

test("keeps conversation configuration aligned and avoids an empty full-height Draft column", () => {
  assert.match(finalLayer, /\.conversation-settings-body\s*>\s*\.studio-form-field\s*\{[\s\S]*?min-width:\s*0;/);
  assert.match(finalLayer, /\.conversation-authoring-layout\[data-draft-state="empty"\][\s\S]*?grid-template-columns:/);
  assert.match(finalLayer, /\.conversation-draft-rail\.is-empty\s*\{[\s\S]*?height:\s*fit-content(?:\s*!important)?;/);
});

test("keeps Lucide geometry square instead of overriding component dimensions globally", () => {
  assert.doesNotMatch(tokens, /svg\s*\{[^}]*width:\s*1em;[^}]*height:\s*1em;/);
  assert.match(finalLayer, /\.navigation-rail \.nav-item svg\s*\{\s*width:\s*18px;\s*height:\s*18px;\s*flex-basis:\s*18px;/);
  assert.match(tokens, /\.agent-avatar-xs svg\s*\{\s*width:\s*12px;\s*height:\s*12px;/);
  assert.match(tokens, /\.agent-avatar-sm svg,[\s\S]*?\.agent-avatar-md svg\s*\{\s*width:\s*16px;\s*height:\s*16px;/);
  assert.match(foundation, /\.chat-run-error-icon svg\s*\{\s*width:\s*17px;\s*height:\s*17px;/);
});

test("keeps cloud versions in a bounded compact grid instead of native radio geometry", () => {
  assert.match(foundation, /\.deployment-version-list\s*\{[\s\S]*?max-height:\s*430px;[\s\S]*?overflow-x:\s*hidden;[\s\S]*?overflow-y:\s*auto;/);
  assert.match(foundation, /\.deployment-version-option\s*\{[\s\S]*?display:\s*grid;[\s\S]*?width:\s*100%;[\s\S]*?min-width:\s*0;[\s\S]*?grid-template-columns:\s*minmax\(160px, 1fr\) minmax\(112px, max-content\) minmax\(180px, 216px\) minmax\(136px, max-content\);/);
  assert.match(foundation, /\.deployment-version-name\s*\{[\s\S]*?overflow:\s*hidden;[\s\S]*?text-overflow:\s*ellipsis;/);
  assert.match(foundation, /\.deployment-version-state,[\s\S]*?overflow:\s*hidden;[\s\S]*?text-overflow:\s*ellipsis;/);
  assert.match(foundation, /\.deployment-version-option\s*>\s*code\s*\{[\s\S]*?overflow:\s*hidden;[\s\S]*?text-overflow:\s*ellipsis;/);
  assert.doesNotMatch(foundation, /\.deployment-version-option\s*>\s*input/);
});

test("keeps the AI conversation primitives compact and free of duplicate composer overrides", () => {
  assert.equal((finalLayer.match(/\.chat-composer\s*\{/g) || []).length, 1);
  assert.match(finalLayer, /\.chat-composer\s*\{[\s\S]*?border-radius:\s*12px;/);
  assert.match(finalLayer, /\.chat-processing-group\s*\{[\s\S]*?border:\s*0;[\s\S]*?background:\s*transparent;/);
  assert.match(finalLayer, /\.chat-code-header\s*\{[\s\S]*?justify-content:\s*space-between;/);
  assert.match(finalLayer, /\.chat-composer-disclaimer\s*\{[\s\S]*?font-size:\s*12px;/);
});

test("uses a single-surface mobile conversation with an off-canvas session drawer", () => {
  assert.match(finalLayer, /@media \(max-width:\s*720px\)\s*\{[\s\S]*?--studio-app-rail:\s*56px;/);
  assert.match(finalLayer, /\.studio-chat-shell \.chat-session-sidebar\s*\{[\s\S]*?transform:\s*translateX\(-104%\);/);
  assert.match(finalLayer, /\.studio-chat-shell\.sessions-open \.chat-session-sidebar\s*\{\s*transform:\s*translateX\(0\);/);
  assert.match(finalLayer, /\.chat-session-backdrop\s*\{[\s\S]*?pointer-events:\s*none;/);
  assert.match(finalLayer, /\.studio-chat-shell\.sessions-open \.chat-session-backdrop\s*\{[\s\S]*?pointer-events:\s*auto;/);
  assert.match(finalLayer, /\.chat-session-mobile-close\s*\{[\s\S]*?display:\s*inline-grid;/);
});

test("keeps mobile Agent creation on one readable column with bounded header actions", () => {
  assert.match(finalLayer, /html,[\s\S]*?body,[\s\S]*?\.app-shell\s*\{\s*overflow-x:\s*clip;/);
  assert.match(finalLayer, /\.app-shell\[data-view="create"\] #pageHeaderActions > \.tag,[\s\S]*?display:\s*none;/);
  assert.match(finalLayer, /\.create-shell \.wizard-panel\s*\{\s*padding:\s*24px 16px 80px;/);
  assert.match(finalLayer, /\.create-shell \.template-grid,[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\);/);
  assert.match(finalLayer, /\.create-shell \.wizard-actions \.summary-chips\s*\{\s*display:\s*none;/);
  assert.match(finalLayer, /#pageHeaderActions \.button\s*\{[\s\S]*?width:\s*40px;/);
  assert.match(finalLayer, /\.app-shell\[data-view="deployments"\] #pageHeaderActions > \.button\.secondary\s*\{\s*display:\s*none;/);
  assert.match(finalLayer, /\.app-shell\[data-view="agent-detail"\] #pageHeaderActions > \.button\.secondary\s*\{\s*display:\s*none;/);
  assert.match(finalLayer, /\.delivery-table-scroll\s*\{[\s\S]*?contain:\s*inline-size paint;/);
});
