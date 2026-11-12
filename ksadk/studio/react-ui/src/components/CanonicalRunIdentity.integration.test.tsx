import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { CompactHarnessTimeline } from "./CompactHarnessTimeline";

// Real shared renderer, activity fallback and clock: the duplicate was hidden
// by fixtures supplying runId and by mocking the activity renderer to null.
vi.mock("../api", () => ({ apiFetch: vi.fn() }));
afterEach(() => vi.resetAllMocks());

it("renders one streaming thinking segment and a header above canonical output without run metadata", async () => {
  vi.mocked(apiFetch).mockImplementation(async path => new Response(JSON.stringify(
    String(path).endsWith("/activities")
      ? { runId: "run-current", status: "running", activities: [
        { id: "reason", kind: "step", category: "reasoning", label: "Analysis", status: "running", details: [] },
      ] }
      : { items: [{ id: "run-current", status: "RUNNING", startedAt: new Date().toISOString() }] },
  )));
  const { container, unmount } = render(<CompactHarnessTimeline agentName="Game builder" isMobile={false}
    isStreaming activity={null} sessionId="session-current" activeRunId="run-current"
    onDeleteFeedback={vi.fn()} onSubmitFeedback={vi.fn()} onRespondToApproval={vi.fn()}
    messages={[
      { id: "conversation:run-current:item-a", role: "model", content: "Preparing the game", timestamp: 1 },
      { id: "conversation:run-current:item-b", role: "model", content: "", timestamp: 2,
        blocks: [{ id: "conversation-block:item-b", type: "thinking", source: "provider_reasoning",
          status: "streaming", content: "Designing a playable level" }] },
    ]} />);
  await waitFor(() => expect(screen.getByTestId("run-elapsed")).toBeVisible());
  expect(container.querySelectorAll('[data-testid="live-thinking"][data-streaming="true"]')).toHaveLength(1);
  expect(container.querySelector(".harness-live-run")).toBeNull();
  expect(screen.getByTestId("run-elapsed").parentElement?.firstElementChild).toBe(screen.getByTestId("run-elapsed"));
  expect(screen.getByTestId("live-thinking")).toHaveTextContent("Designing a playable level");
  unmount();
});
