import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { transformWithOxc } from "vite";

const apiSource = await readFile(new URL("./api.ts", import.meta.url), "utf8");

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function loadApi({ hash, fetch }) {
  const replacedUrls = [];
  globalThis.window = {
    fetch,
    location: {
      hash,
      href: `http://127.0.0.1:5175/${hash}`,
      origin: "http://127.0.0.1:5175",
      pathname: "/",
      search: "",
    },
    history: {
      replaceState(_state, _title, url) {
        replacedUrls.push(url);
      },
    },
  };

  const transformed = await transformWithOxc(apiSource, "api.ts", { lang: "ts" });
  const uniqueSource = `${transformed.code}\n// test-instance-${Math.random()}`;
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(uniqueSource).toString("base64")}`;
  return { api: await import(moduleUrl), replacedUrls };
}

test("session fragment is exchanged before API writes and removed from the URL", async () => {
  const requests = [];
  const { api, replacedUrls } = await loadApi({
    hash: "#session=cli-session-token",
    fetch: async (input, init = {}) => {
      requests.push({ input: String(input), init });
      if (String(input).endsWith("/api/v1/system/session")) {
        return jsonResponse({ csrfToken: "csrf-from-fragment" });
      }
      return jsonResponse({ error: { code: "NOT_FOUND" } }, 404);
    },
  });

  await api.initializeStudioSession();
  await api.apiFetch("/api/v1/write-probe", { method: "POST" });

  assert.equal(requests.length, 2);
  assert.equal(
    JSON.parse(requests[0].init.body).token,
    "cli-session-token",
  );
  assert.equal(
    new Headers(requests[1].init.headers).get("X-CSRF-Token"),
    "csrf-from-fragment",
  );
  assert.equal(requests[1].init.credentials, "same-origin");
  assert.deepEqual(replacedUrls, ["/"]);
});

test("an existing session cookie recovers its CSRF token from bootstrap", async () => {
  const requests = [];
  const { api } = await loadApi({
    hash: "#/agents",
    fetch: async (input, init = {}) => {
      requests.push({ input: String(input), init });
      if (String(input).endsWith("/api/v1/system/bootstrap")) {
        return jsonResponse({ csrfToken: "csrf-from-cookie" });
      }
      return jsonResponse({ error: { code: "NOT_FOUND" } }, 404);
    },
  });

  await api.initializeStudioSession();
  await api.apiFetch("/api/v1/write-probe", { method: "DELETE" });

  assert.equal(
    new Headers(requests[1].init.headers).get("X-CSRF-Token"),
    "csrf-from-cookie",
  );
});

test("OpenAI-compatible response writes receive the same Studio CSRF protection", async () => {
  const requests = [];
  const { api } = await loadApi({
    hash: "#/conversations",
    fetch: async (input, init = {}) => {
      requests.push({ input: String(input), init });
      if (String(input).endsWith("/api/v1/system/bootstrap")) {
        return jsonResponse({ csrfToken: "csrf-for-responses" });
      }
      return jsonResponse({ status: "accepted" }, 202);
    },
  });

  await api.initializeStudioSession();
  await api.apiFetch("/v1/responses/resp-1:pause", { method: "POST" });

  assert.equal(
    new Headers(requests[1].init.headers).get("X-CSRF-Token"),
    "csrf-for-responses",
  );
  assert.equal(requests[1].init.credentials, "same-origin");
});

test("a stale CSRF token is refreshed once before retrying a local write", async () => {
  const requests = [];
  let bootstrapCount = 0;
  const { api } = await loadApi({
    hash: "#/agents",
    fetch: async (input, init = {}) => {
      const request = { input: String(input), init };
      requests.push(request);
      if (request.input.endsWith("/api/v1/system/bootstrap")) {
        bootstrapCount += 1;
        return jsonResponse({
          csrfToken: bootstrapCount === 1 ? "stale-csrf" : "fresh-csrf",
        });
      }
      if (new Headers(init.headers).get("X-CSRF-Token") === "stale-csrf") {
        return jsonResponse(
          { error: { code: "CSRF_TOKEN_INVALID", message: "stale" } },
          403,
        );
      }
      return jsonResponse({ saved: true });
    },
  });

  await api.initializeStudioSession();
  const response = await api.apiFetch("/api/v1/system/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sandbox: "read-only" }),
  });

  assert.equal(response.status, 200);
  assert.equal(requests.length, 4);
  assert.equal(
    new Headers(requests[3].init.headers).get("X-CSRF-Token"),
    "fresh-csrf",
  );
});
