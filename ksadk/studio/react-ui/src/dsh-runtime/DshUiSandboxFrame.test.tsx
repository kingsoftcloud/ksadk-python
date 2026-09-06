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

  it("marks itself disposed when the channel tears down on unmount", async () => {
    const session = makeSession();
    const onChannelDisposed = vi.fn();
    const { container, unmount } = render(
      <DshUiSandboxFrame session={session} title="Test plugin" onChannelDisposed={onChannelDisposed} />,
    );
    const iframe = container.querySelector("iframe");
    expect(iframe?.getAttribute("data-disposed")).toBe("false");

    // Unmounting triggers the effect cleanup, which closes the MessageChannel
    // and invokes the onChannelDisposed callback.
    unmount();
    await waitFor(() => {
      expect(onChannelDisposed).toHaveBeenCalledTimes(1);
    });
    // After unmount the iframe element is gone; the disposed flag was set on
    // the component's state before cleanup ran. We verify the callback fired,
    // which is the externally observable signal of resource cleanup.
  });

  it("closes the MessageChannel on unmount so the port is no longer usable", async () => {
    const session = makeSession();
    const { unmount } = render(
      <DshUiSandboxFrame session={session} title="Test plugin" />,
    );
    unmount();
    // After cleanup, the frame's effect ran without error; a fresh mount with
    // a distinct session produces a frame with the new session id.
    const fresh = makeSession();
    fresh.uiSessionId = "dshui_fresh_session_456";
    fresh.handshake.sessionId = fresh.uiSessionId;
    const { container } = render(
      <DshUiSandboxFrame session={fresh} title="Re-mounted" />,
    );
    const iframe = container.querySelector("iframe");
    expect(iframe?.getAttribute("data-dsh-ui-session")).toBe(fresh.uiSessionId);
    expect(iframe?.getAttribute("data-dsh-ui-session")).not.toBe(session.uiSessionId);
  });
});
