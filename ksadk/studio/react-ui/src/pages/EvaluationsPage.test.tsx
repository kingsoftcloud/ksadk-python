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
    expect(screen.getByLabelText(/Agent 地址/)).toBeInTheDocument();
    expect(await screen.findByRole("combobox", { name: "EvalSet 文件" })).toHaveTextContent("smoke · 2 Cases");
    await user.click(screen.getByRole("combobox", { name: "Target 类型" }));
    await user.click(await screen.findByRole("option", { name: "本地源码" }));
    expect(screen.getByLabelText(/Agent 源码目录/)).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Target 类型" }));
    await user.click(await screen.findByRole("option", { name: "Studio Build" }));
    expect(screen.getByRole("combobox", { name: "Studio Build" })).toHaveTextContent("agent-1 · langgraph");
  });

  it("offers immutable cloud Dataset versions as an evaluation source", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async input => {
      const url = String(input);
      if (url === "/api/v1/evaluation-targets") return response({ evalsets: [], builds: [] });
      if (url === "/api/v1/evaluation-cloud/catalog") {
        return response({
          items: [{
            datasetId: "dataset-1",
            name: "support",
            version: 4,
            schemaHash: "a".repeat(64),
            contentDigest: "b".repeat(64),
            rowCount: 2,
          }],
        });
      }
      return response({ items: [] });
    });
    render(<EvaluationsPage refreshTick={0} />);

    await user.click(screen.getByRole("button", { name: "新建评测" }));
    await user.click(await screen.findByRole("combobox", { name: "Dataset source" }));
    await user.click(await screen.findByRole("option", { name: "Cloud Dataset version" }));

    expect(screen.getByRole("combobox", { name: "Cloud Dataset" })).toHaveTextContent("support - v4");
    expect(document.querySelector("#evaluation-dataset-version")).not.toBeInTheDocument();
  });

  it("selects an exact cloud Dataset version when a Dataset has multiple snapshots", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async input => {
      const url = String(input);
      if (url === "/api/v1/evaluation-targets") return response({ evalsets: [], builds: [] });
      if (url === "/api/v1/evaluation-cloud/catalog") {
        return response({
          items: [
            { datasetId: "dataset-1", name: "support", version: 4, schemaHash: "a".repeat(64), contentDigest: "b".repeat(64), rowCount: 2 },
            { datasetId: "dataset-1", name: "support", version: 5, schemaHash: "c".repeat(64), contentDigest: "d".repeat(64), rowCount: 3 },
          ],
        });
      }
      return response({ items: [] });
    });
    render(<EvaluationsPage refreshTick={0} />);

    await user.click(screen.getByRole("button", { name: "新建评测" }));
    await user.click(await screen.findByRole("combobox", { name: "Dataset source" }));
    await user.click(await screen.findByRole("option", { name: "Cloud Dataset version" }));
    await vi.waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Cloud Dataset" })).toHaveTextContent("support - v4");
    });
    await user.click(screen.getByRole("combobox", { name: "Cloud Dataset" }));
    const options = await screen.findAllByRole("option");
    expect(options).toHaveLength(2);
    expect(options[1]).toHaveTextContent("support - v5");
    await user.click(options[1]);

    expect(document.querySelector("#evaluation-dataset-version")).not.toBeInTheDocument();
  });

  it("submits the selected immutable cloud Dataset reference", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/v1/evaluation-targets") return response({ evalsets: [], builds: [] });
      if (url === "/api/v1/evaluation-cloud/catalog") {
        return response({
          items: [{
            datasetId: "dataset-1",
            name: "support",
            projectId: "project-1",
            version: 5,
            schemaHash: "c".repeat(64),
            contentDigest: "d".repeat(64),
            rowCount: 3,
          }],
        });
      }
      if (url === "/api/v1/evaluations" && init?.method === "POST") {
        return response({ id: "op_eval_cloud", status: "QUEUED" });
      }
      if (url === "/api/v1/operations/op_eval_cloud") {
        return response({ id: "op_eval_cloud", status: "FAILED", error: { message: "test completion" } });
      }
      return response({ items: [] });
    });
    render(<EvaluationsPage refreshTick={0} />);

    await user.click(screen.getByRole("button", { name: "新建评测" }));
    await user.click(await screen.findByRole("combobox", { name: "Dataset source" }));
    await user.click(await screen.findByRole("option", { name: "Cloud Dataset version" }));
    await vi.waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Cloud Dataset" })).toHaveTextContent("support - v5");
    });
    await user.type(screen.getByLabelText(/Agent 地址/), "https://agent.example.test/a2a");
    await user.click(screen.getByRole("button", { name: "开始评测" }));

    await vi.waitFor(() => {
      const post = mockedFetch.mock.calls.find(([, init]) => init?.method === "POST");
      expect(post).toBeDefined();
      expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
        cloudDataset: {
          provider: "agent-eval/evalsmith",
          projectId: "project-1",
          datasetId: "dataset-1",
          version: 5,
          schemaHash: "c".repeat(64),
          contentDigest: "d".repeat(64),
          rowCount: 3,
        },
      });
    });
  });

  it("submits the shared evaluation contract and waits for its operation", async () => {
    const user = userEvent.setup();
    const completedReport = {
      schemaVersion: "ksadk.eval.report/v1",
      spec: {
        id: "eval_run_1",
        evalset: { name: "smoke" },
        target: { kind: "a2a", runtime: "a2a", revisionDigest: "sha256:test" },
      },
      status: "PASSED",
      createdAt: "2026-08-13T00:00:00Z",
      summary: {
        totalCases: 1,
        passedCases: 1,
        failedCases: 0,
        unavailableCases: 0,
        errorCases: 0,
        cancelledCases: 0,
      },
      caseRuns: [{
        caseId: "one",
        targetRun: { status: "PASSED", output: "answer", durationMs: 1, traceRefs: [] },
        metrics: [],
      }],
    };
    mockedFetch.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/v1/evaluations" && init?.method === "POST") {
        return response({ id: "op_eval_1", status: "QUEUED" });
      }
      if (url === "/api/v1/operations/op_eval_1") {
        return response({ id: "op_eval_1", status: "SUCCEEDED", resourceId: "eval_run_1" });
      }
      if (url === "/api/v1/evaluations/eval_run_1") {
        return response(completedReport);
      }
      return response({ items: [] });
    });
    render(<EvaluationsPage refreshTick={0} />);

    await user.click(screen.getByRole("button", { name: "新建评测" }));
    await user.type(screen.getByLabelText(/EvalSet 文件/), "evalsets/smoke.yaml");
    await user.type(screen.getByLabelText(/Agent 地址/), "https://agent.example.test/a2a");
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
    expect(mockedFetch).toHaveBeenCalledWith("/api/v1/evaluations/eval_run_1");
    expect(await screen.findByText("answer")).toBeInTheDocument();
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
    await user.type(screen.getByLabelText(/Agent 地址/), "https://agent.example.test/a2a");
    await user.click(screen.getByRole("button", { name: "开始评测" }));
    await vi.waitFor(() => expect(pollingSignal).toBeDefined());

    page.unmount();

    expect(pollingSignal?.aborted).toBe(true);
  });

  it("shows the current evaluation case from operation events", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/v1/evaluations" && init?.method === "POST") {
        return response({ id: "op_eval_progress", status: "QUEUED" });
      }
      if (url === "/api/v1/operations/op_eval_progress") {
        return response({ id: "op_eval_progress", status: "RUNNING" });
      }
      if (url === "/api/v1/operations/op_eval_progress/events?after=0") {
        return response({ items: [{ id: 3, type: "evaluation.case.started", data: { caseId: "case-2", index: 2, total: 3 } }] });
      }
      return url === "/api/v1/evaluation-targets"
        ? response({ evalsets: [], builds: [] })
        : response({ items: [] });
    });
    render(<EvaluationsPage refreshTick={0} />);

    await user.click(screen.getByRole("button", { name: "新建评测" }));
    await user.type(screen.getByLabelText(/EvalSet 文件/), "evalsets/smoke.yaml");
    await user.type(screen.getByLabelText(/Agent 地址/), "https://agent.example.test/a2a");
    await user.click(screen.getByRole("button", { name: "开始评测" }));

    expect(await screen.findByText("Case 2 / 3：case-2")).toBeInTheDocument();
  });

  it("cancels the current evaluation operation", async () => {
    const user = userEvent.setup();
    let cancelled = false;
    const cancelledReport = {
      schemaVersion: "ksadk.eval.report/v1",
      spec: {
        id: "eval_cancelled",
        evalset: { name: "smoke" },
        target: { kind: "a2a", runtime: "a2a", revisionDigest: "sha256:test" },
      },
      status: "CANCELLED",
      createdAt: "2026-08-13T00:00:00Z",
      summary: {
        totalCases: 1,
        passedCases: 1,
        failedCases: 0,
        unavailableCases: 0,
        errorCases: 0,
        cancelledCases: 0,
      },
      caseRuns: [{
        caseId: "one",
        targetRun: { status: "PASSED", output: "partial answer", durationMs: 1, traceRefs: [] },
        metrics: [],
      }],
    };
    mockedFetch.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/v1/evaluations" && init?.method === "POST") {
        return response({ id: "op_eval_cancel", status: "QUEUED" });
      }
      if (url === "/api/v1/operations/op_eval_cancel:cancel" && init?.method === "POST") {
        cancelled = true;
        return response({ id: "op_eval_cancel", status: "CANCELLED" });
      }
      if (url === "/api/v1/operations/op_eval_cancel") {
        return response({
          id: "op_eval_cancel",
          status: cancelled ? "CANCELLED" : "RUNNING",
          resourceId: "eval_cancelled",
        });
      }
      if (url === "/api/v1/evaluations/eval_cancelled") return response(cancelledReport);
      if (url.startsWith("/api/v1/operations/op_eval_cancel/events")) return response({ items: [] });
      return url === "/api/v1/evaluation-targets"
        ? response({ evalsets: [], builds: [] })
        : response({ items: [] });
    });
    render(<EvaluationsPage refreshTick={0} />);

    await user.click(screen.getByRole("button", { name: "新建评测" }));
    await user.type(screen.getByLabelText(/EvalSet 文件/), "evalsets/smoke.yaml");
    await user.type(screen.getByLabelText(/Agent 地址/), "https://agent.example.test/a2a");
    await user.click(screen.getByRole("button", { name: "开始评测" }));
    await user.click(await screen.findByRole("button", { name: "取消评测" }));

    expect(mockedFetch).toHaveBeenCalledWith(
      "/api/v1/operations/op_eval_cancel:cancel",
      expect.objectContaining({ method: "POST" }),
    );
    expect(await screen.findByText("partial answer")).toBeInTheDocument();
    expect(screen.queryByText(/评测任务状态：CANCELLED/)).not.toBeInTheDocument();
  });
});
