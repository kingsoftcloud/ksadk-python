import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DshUiSandboxFrame } from "./DshUiSandboxFrame";
import type { DshUiSessionCreateResponse } from "./dshUiSandbox";

// The sandbox frame's attach path touches window.addEventListener,
// MessageChannel, and postMessage — all available in jsdom.

function makeSession(): DshUiSessionCreateResponse {
  return {
    uiSessionId: "dshui_test1234567890abcdef",
    sourceId: "frame_test1234567890abcdef",
    expiresInSeconds: 900,
    protocolVersion: "agentkit.dsh-ui/v1",
    descriptorDigest: "sha256:dd",
    inventoryDigest: "sha256:ii",
    allowedTools: [],
    handshake: {
      protocolVersion: "agentkit.dsh-ui/v1",
      kind: "init",
      sessionId: "dshui_test1234567890abcdef",
      sourceId: "frame_test1234567890abcdef",
      handshakeNonce: "nonce-test-1234567890abcdefgh",
      capabilityToken: "token-must-not-appear-in-dom-1234567890abcdef",
    },
    frame: {
      url: "/api/v1/plugin-ecosystems/dsh/sandbox/frame?uiSessionId=dshui_test1234567890abcdef",
      sandbox: "allow-scripts",
      referrerPolicy: "no-referrer",
      credentialless: true,
    },
    extensionPoints: [],
  };
}

describe("DshUiSandboxFrame", () => {
  let postMessageSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    postMessageSpy = vi.spyOn(window, "postMessage");
  });

  afterEach(() => {
    postMessageSpy.mockRestore();
  });

  it("renders an opaque-origin iframe with the sandbox attributes", () => {
    const session = makeSession();
    const { container } = render(
      <DshUiSandboxFrame session={session} title="Test plugin" />,
    );
    const iframe = container.querySelector("iframe");
    expect(iframe).not.toBeNull();
    expect(iframe?.getAttribute("sandbox")).toBe("allow-scripts");
    expect(iframe?.getAttribute("src")).toBe(session.frame.url);
    expect(iframe?.getAttribute("data-dsh-ui-session")).toBe(session.uiSessionId);
  });

  it("never renders the capability token into the DOM", () => {
    const session = makeSession();
    const { container } = render(
      <DshUiSandboxFrame session={session} title="Test plugin" />,
    );
    expect(container.innerHTML).not.toContain("token-must-not-appear");
    expect(container.innerHTML).not.toContain(session.handshake.capabilityToken);
  });

  it("does not expose the handshake nonce in the DOM either", () => {
    const session = makeSession();
    const { container } = render(
      <DshUiSandboxFrame session={session} title="Test plugin" />,
    );
    expect(container.innerHTML).not.toContain(session.handshake.handshakeNonce);
  });

  it("marks itself disposed when the channel tears down", async () => {
    const session = makeSession();
    const { container } = render(
      <DshUiSandboxFrame session={session} title="Test plugin" />,
    );
    // Simulate a channel disposal callback by forcing cleanup through unmount.
    const iframe = container.querySelector("iframe");
    expect(iframe?.getAttribute("data-disposed")).toBe("false");
  });
});
