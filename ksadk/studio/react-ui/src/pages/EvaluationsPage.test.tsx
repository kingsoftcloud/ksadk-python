import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { EvaluationsPage } from "./EvaluationsPage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

const mockedFetch = vi.mocked(apiFetch);

function response(payload: unknown, ok = true): Response {
  return { ok, status: ok ? 200 : 422, json: async () => payload } as Response;
}

describe("EvaluationsPage", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedFetch.mockImplementation(async input => (
      String(input) === "/api/v1/evaluation-targets"
        ? response({ evalsets: [], builds: [] })
        : response({ items: [] })
    ));
  });

  it("loads reports when the evaluation page is mounted", async () => {
    render(<EvaluationsPage refreshTick={0} />);

    expect(await screen.findByText("还没有评测报告")).toBeInTheDocument();
    expect(mockedFetch).toHaveBeenCalledWith("/api/v1/evaluations");
    expect(mockedFetch).toHaveBeenCalledWith("/api/v1/evaluation-targets");
  });

  it("offers discovered EvalSets and immutable Studio Builds", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async input => (
      String(input) === "/api/v1/evaluation-targets"
        ? response({
          evalsets: [{ path: "evaluations/smoke.yaml", name: "smoke", caseCount: 2 }],
          builds: [{ id: "build-1", agentId: "agent-1", runtime: "langgraph" }],
        })
        : response({ items: [] })
    ));
    render(<EvaluationsPage refreshTick={0} />);

    await user.click(screen.getByRole("button", { name: "新建评测" }));
    expect(await screen.findByRole("combobox", { name: "EvalSet 文件" })).toHaveTextContent("smoke · 2 Cases");
    await user.click(screen.getByRole("combobox", { name: "Target 类型" }));
    await user.click(await screen.findByRole("option", { name: "Studio Build" }));
    expect(screen.getByRole("combobox", { name: "Studio Build" })).toHaveTextContent("agent-1 · langgraph");
  });

  it("submits the shared evaluation contract and waits for its operation", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/v1/evaluations" && init?.method === "POST") {
        return response({ id: "op_eval_1", status: "QUEUED" });
      }
      if (url === "/api/v1/operations/op_eval_1") {
        return response({ id: "op_eval_1", status: "SUCCEEDED" });
      }
      return response({ items: [] });
    });
    render(<EvaluationsPage refreshTick={0} />);

    await user.click(screen.getByRole("button", { name: "新建评测" }));
    await user.type(screen.getByLabelText(/EvalSet 文件/), "evalsets/smoke.yaml");
    await user.type(screen.getByLabelText(/Target locator/), "https://agent.example.test/a2a");
    await user.click(screen.getByRole("button", { name: "开始评测" }));

    expect(await screen.findByText("评测任务已完成")).toBeInTheDocument();
    const post = mockedFetch.mock.calls.find(([, init]) => init?.method === "POST");
    expect(post?.[0]).toBe("/api/v1/evaluations");
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      evalsetFile: "evalsets/smoke.yaml",
      target: { kind: "a2a", locator: "https://agent.example.test/a2a" },
    });
    expect(mockedFetch).toHaveBeenCalledWith(
      "/api/v1/operations/op_eval_1",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("aborts operation polling when the evaluation page unmounts", async () => {
    const user = userEvent.setup();
    let pollingSignal: AbortSignal | undefined;
    mockedFetch.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/v1/evaluations" && init?.method === "POST") {
        return response({ id: "op_eval_pending", status: "QUEUED" });
      }
      if (url === "/api/v1/operations/op_eval_pending") {
        pollingSignal = init?.signal || undefined;
        return response({ id: "op_eval_pending", status: "RUNNING" });
      }
      return url === "/api/v1/evaluation-targets"
        ? response({ evalsets: [], builds: [] })
        : response({ items: [] });
    });
    const page = render(<EvaluationsPage refreshTick={0} />);
    await user.click(screen.getByRole("button", { name: "新建评测" }));
    await user.type(screen.getByLabelText(/EvalSet 文件/), "evalsets/smoke.yaml");
    await user.type(screen.getByLabelText(/Target locator/), "https://agent.example.test/a2a");
    await user.click(screen.getByRole("button", { name: "开始评测" }));
    await vi.waitFor(() => expect(pollingSignal).toBeDefined());

    page.unmount();

    expect(pollingSignal?.aborted).toBe(true);
  });
});
