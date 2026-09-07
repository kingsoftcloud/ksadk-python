import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { EvaluationsPage } from "./EvaluationsPage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

const mockedFetch = vi.mocked(apiFetch);
const uploadedEvalsetPath = "evaluations/uploads/abc-smoke.yaml";

function response(payload: unknown, ok = true): Response {
  return { ok, status: ok ? 200 : 422, json: async () => payload } as Response;
}

function isEvalsetUpload(input: Parameters<typeof apiFetch>[0], init?: Parameters<typeof apiFetch>[1]): boolean {
  return String(input) === "/api/v1/evaluation-files" && init?.method === "POST";
}

async function uploadEvalset(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.upload(
    screen.getByLabelText("选择 EvalSet 文件"),
    new File(["schemaVersion: ksadk.eval/v1"], "smoke.yaml", { type: "application/yaml" }),
  );
  await screen.findByText(uploadedEvalsetPath);
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

    expect(await screen.findByText("还没有评测任务")).toBeInTheDocument();
    expect(mockedFetch).toHaveBeenCalledWith("/api/v1/evaluation-runs", expect.anything());
    expect(mockedFetch).toHaveBeenCalledWith("/api/v1/evaluation-targets");
    expect(screen.getByRole("region", { name: "评测运行" }).querySelectorAll(".evaluation-page__panel-header")).toHaveLength(1);
  });

  it("uses the shared aligned form grid and business target labels", async () => {
    const user = userEvent.setup();
    render(<EvaluationsPage refreshTick={0} />);

    await user.click(screen.getByRole("button", { name: "新建评测" }));

    const form = document.getElementById("evaluation-create-form");
    expect(form).toHaveClass("form-grid", "two-columns");
    expect(screen.getByLabelText(/Agent 地址/)).toBeInTheDocument();

    await user.click(screen.getByRole("combobox", { name: "Target 类型" }));
    await user.click(await screen.findByRole("option", { name: "本地源码" }));
    expect(screen.getByLabelText(/Agent 源码目录/)).toBeInTheDocument();
  });

  it("uploads a selected EvalSet and keeps its workspace path", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async (input, init) => (
      isEvalsetUpload(input, init)
        ? response({
          path: uploadedEvalsetPath,
          name: "smoke",
          caseCount: 1,
        })
        : String(input) === "/api/v1/evaluation-targets"
          ? response({ evalsets: [], builds: [] })
          : response({ items: [] })
    ));
    render(<EvaluationsPage refreshTick={0} />);

    await user.click(screen.getByRole("button", { name: "新建评测" }));
    expect(screen.getByText("运行策略")).toBeInTheDocument();
    await user.upload(
      screen.getByLabelText("选择 EvalSet 文件"),
      new File(["schemaVersion: ksadk.eval/v1"], "smoke.yaml", { type: "application/yaml" }),
    );

    expect(await screen.findByText(uploadedEvalsetPath)).toBeInTheDocument();
    expect(mockedFetch).toHaveBeenCalledWith(
      "/api/v1/evaluation-files",
      expect.objectContaining({ method: "POST", body: expect.any(FormData) }),
    );
  });

  it("offers Studio Agents and immutable Studio Builds", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async input => (
      String(input) === "/api/v1/agents"
        ? response({
          items: [
            { metadata: { id: "agent-1", name: "Agent One" } },
            { metadata: { id: "agent-2", name: "Agent Two" } },
          ],
        })
        : String(input) === "/api/v1/evaluation-targets"
        ? response({
          evalsets: [{ path: "evaluations/smoke.yaml", name: "smoke", caseCount: 2 }],
          builds: [
            { id: "build-2", agentId: "agent-1", runtime: "langgraph" },
            { id: "build-1", agentId: "agent-1", runtime: "langgraph" },
            { id: "build-3", agentId: "agent-2", runtime: "adk" },
          ],
        })
        : response({ items: [] })
    ));
    render(<EvaluationsPage refreshTick={0} />);

    await user.click(screen.getByRole("button", { name: "新建评测" }));
    await user.click(screen.getByRole("combobox", { name: "Target 类型" }));
    await user.click(await screen.findByRole("option", { name: "Studio Build" }));
    expect(screen.getByRole("combobox", { name: "Studio Agent" })).toHaveTextContent("Agent One");
    expect(screen.getByRole("combobox", { name: "Studio Build" })).toHaveTextContent("build-2");

    await user.click(screen.getByRole("combobox", { name: "Studio Agent" }));
    await user.click(await screen.findByRole("option", { name: /Agent Two/ }));

    expect(screen.getByRole("combobox", { name: "Studio Build" })).toHaveTextContent("build-3");
    expect(mockedFetch).toHaveBeenCalledWith("/api/v1/agents");
  });

  it("submits the shared evaluation contract and returns to the run list", async () => {
    const user = userEvent.setup();
    let submitted = false;
    const queuedRun = {
      id: "eval_run_1",
      operationId: "op_eval_1",
      status: "QUEUED",
      createdAt: "2026-08-13T00:00:00Z",
      completedAt: null,
      evalset: { name: "smoke", caseCount: 1 },
      target: { kind: "a2a", label: "A2A Agent" },
      evaluators: [],
      progress: null,
      summary: null,
      hasReport: false,
      error: null,
    };
    mockedFetch.mockImplementation(async (input, init) => {
      const url = String(input);
      if (isEvalsetUpload(input, init)) return response({ path: uploadedEvalsetPath });
      if (url === "/api/v1/evaluations" && init?.method === "POST") {
        submitted = true;
        return response({ id: "op_eval_1", status: "QUEUED", resourceId: "eval_run_1" });
      }
      if (url === "/api/v1/evaluation-runs") return response({ items: submitted ? [queuedRun] : [] });
      return response({ items: [] });
    });
    render(<EvaluationsPage refreshTick={0} />);

    await user.click(screen.getByRole("button", { name: "新建评测" }));
    expect(screen.getByRole("checkbox", { name: "响应契约" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "运行预算" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "工具轨迹" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "参考答案匹配" })).not.toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: "工具轨迹" }));
    await user.click(screen.getByRole("checkbox", { name: "参考答案匹配" }));
    await uploadEvalset(user);
    await user.type(screen.getByLabelText(/Agent 地址/), "https://agent.example.test/a2a");
    await user.click(screen.getByRole("button", { name: "开始评测" }));

    expect(await screen.findByText("评测任务已创建")).toBeInTheDocument();
    expect(await screen.findByText("smoke")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "新建评测" })).not.toBeInTheDocument();
    const post = mockedFetch.mock.calls.find(([input, init]) => (
      String(input) === "/api/v1/evaluations" && init?.method === "POST"
    ));
    expect(post?.[0]).toBe("/api/v1/evaluations");
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      evalsetFile: uploadedEvalsetPath,
      target: { kind: "a2a", locator: "https://agent.example.test/a2a" },
      config: {
        evaluators: [
          "response_contract@v1",
          "runtime_budget@v1",
          "reference_match@v1",
        ],
      },
    });
    expect(mockedFetch.mock.calls.some(([input]) => String(input).includes("/api/v1/operations/"))).toBe(false);
  });

  it("shows persisted progress for running evaluations", async () => {
    mockedFetch.mockImplementation(async input => String(input) === "/api/v1/evaluation-runs"
      ? response({ items: [{
        id: "eval_progress",
        operationId: "op_progress",
        status: "RUNNING",
        createdAt: "2026-08-13T00:00:00Z",
        completedAt: null,
        evalset: { name: "progress", caseCount: 3 },
        target: { kind: "a2a", label: "A2A Agent" },
        evaluators: [],
        progress: { current: 2, total: 3, caseId: "case-2" },
        summary: null,
        hasReport: false,
        error: null,
      }] })
      : String(input) === "/api/v1/evaluation-targets"
        ? response({ evalsets: [], builds: [] })
        : response({ items: [] }));
    render(<EvaluationsPage refreshTick={0} />);

    expect(await screen.findByText("2 / 3")).toBeInTheDocument();
    expect(screen.getByText("case-2")).toBeInTheDocument();
  });
});
