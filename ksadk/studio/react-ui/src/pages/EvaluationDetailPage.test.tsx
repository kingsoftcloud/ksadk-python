import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { EvaluationDetailPage } from "./EvaluationDetailPage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

const mockedFetch = vi.mocked(apiFetch);

function response(payload: unknown, ok = true): Response {
  return { ok, status: ok ? 200 : 422, json: async () => payload } as Response;
}

describe("EvaluationDetailPage", () => {
  beforeEach(() => mockedFetch.mockReset());

  it("shows running progress and cancels the operation", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/v1/operations/op_1:cancel" && init?.method === "POST") {
        return response({ status: "CANCELLED" });
      }
      return response({
        id: "eval_1",
        operationId: "op_1",
        status: "RUNNING",
        createdAt: "2026-08-18T00:00:00Z",
        completedAt: null,
        evalset: { name: "smoke", caseCount: 3 },
        target: { kind: "a2a", label: "A2A Agent" },
        evaluators: ["response_contract@v1"],
        progress: { current: 2, total: 3, caseId: "case-2" },
        summary: null,
        hasReport: false,
        error: null,
        report: null,
      });
    });

    render(<EvaluationDetailPage runId="eval_1" onBack={vi.fn()} />);

    expect(await screen.findByText("2 / 3")).toBeInTheDocument();
    expect(screen.getByText("case-2")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "取消评测" }));
    expect(mockedFetch).toHaveBeenCalledWith(
      "/api/v1/operations/op_1:cancel",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("shows the immutable dataset snapshot alongside case results", async () => {
    mockedFetch.mockResolvedValue(response({
      id: "eval_2",
      operationId: "op_2",
      status: "PASSED",
      createdAt: "2026-08-18T00:00:00Z",
      completedAt: "2026-08-18T00:00:02Z",
      evalset: { name: "context-regression", caseCount: 1 },
      target: { kind: "studio_build", label: "Context Agent" },
      evaluators: ["response_contract@v1"],
      progress: null,
      summary: {
        totalCases: 1,
        passedCases: 1,
        failedCases: 0,
        unavailableCases: 0,
        errorCases: 0,
        cancelledCases: 0,
      },
      hasReport: true,
      error: null,
      report: {
        spec: {
          id: "eval_2",
          evalset: {
            schemaVersion: "ksadk.eval/v1",
            name: "context-regression",
            sourceFormat: "native",
            contentDigest: "sha256:dataset-snapshot",
            metadata: { description: "多轮上下文回归数据集" },
            cases: [{
              id: "remember-code",
              metadata: { priority: "P0" },
              turns: [
                {
                  input: "请记住验证码 KSA-731。",
                  expectedTools: [{ name: "memory_lookup" }],
                  metadata: {},
                },
                {
                  input: "刚才的验证码是什么？",
                  expectedOutput: "KSA-731",
                  expectedTools: [],
                  metadata: {},
                },
              ],
              assertions: [{ type: "response.equals", value: "KSA-731", required: true }],
            }],
          },
          target: {
            kind: "studio_build",
            entrypoint: "build:build-1",
            revisionDigest: "sha256:build-1",
            runtime: "langgraph",
          },
        },
        status: "PASSED",
        createdAt: "2026-08-18T00:00:00Z",
        summary: {
          totalCases: 1,
          passedCases: 1,
          failedCases: 0,
          unavailableCases: 0,
          errorCases: 0,
          cancelledCases: 0,
        },
        caseRuns: [{
          caseId: "remember-code",
          attempt: 1,
          targetRun: {
            status: "PASSED",
            output: "KSA-731",
            durationMs: 620,
            usage: { inputTokens: 20, outputTokens: 4, totalTokens: 24, reported: true },
            toolCalls: [],
            traceRef: { traceId: "trace-1" },
          },
          metrics: [{
            name: "response_contract",
            version: "v1",
            status: "PASS",
            score: 1,
            required: true,
            evidence: { assertion: "response.equals" },
          }],
        }],
      },
    }));

    render(<EvaluationDetailPage runId="eval_2" onBack={vi.fn()} />);

    expect(await screen.findByRole("heading", { name: "数据集快照" })).toBeInTheDocument();
    expect(screen.getByText("多轮上下文回归数据集")).toBeInTheDocument();
    expect(screen.getByText("sha256:dataset-snapshot")).toBeInTheDocument();
    expect(screen.getByText("Turn 1")).toBeInTheDocument();
    expect(screen.getByText("请记住验证码 KSA-731。")).toBeInTheDocument();
    expect(screen.getByText("Turn 2")).toBeInTheDocument();
    expect(screen.getAllByText("刚才的验证码是什么？")).toHaveLength(2);
    expect(screen.getByText("memory_lookup")).toBeInTheDocument();
    expect(screen.getByText("response.equals")).toBeInTheDocument();
  });
});
