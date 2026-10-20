import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { ModelSetupForm } from "./ModelSetupForm";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);

const PROBE_PAYLOAD = {
  attempts: [
    { protocol: "chat", endpointUrl: "https://kspmas.ksyun.com/v1/chat/completions", status: "unavailable" },
    { protocol: "anthropic", endpointUrl: "https://kspmas.ksyun.com/v1/messages", status: "ok", latencyMs: 120 },
  ],
  models: ["BAAI/bge-m3", "deepseek-v4.1-flash", "glm-5.1"],
  recommended: { protocol: "anthropic", wireApi: "anthropic", endpointUrl: "https://kspmas.ksyun.com/v1/messages", status: "ok" },
};

beforeEach(() => {
  fetchMock.mockReset();
});

describe("ModelSetupForm", () => {
  it("probes with the default endpoint when the address is blank and preselects a chat model", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (path) => (
      String(path).includes("model-endpoints:probe")
        ? { ok: true, json: async () => PROBE_PAYLOAD }
        : { ok: true, json: async () => ({ items: [] }) }
    ) as Response);
    render(<ModelSetupForm onSaved={vi.fn()} />);

    await user.type(screen.getByLabelText(/^API Key/, { selector: "input" }), "sk-test-12345678");
    await user.click(screen.getByRole("button", { name: /重新探测/ }));

    await waitFor(() => {
      const probeCall = fetchMock.mock.calls.find(([path]) => String(path).includes("model-endpoints:probe"));
      expect(probeCall).toBeDefined();
    });
    const probeCall = fetchMock.mock.calls.find(([path]) => String(path).includes("model-endpoints:probe"));
    const probeBody = JSON.parse(String(probeCall?.[1]?.body));
    // 未填地址时默认探测 kspmas 端点。
    expect(probeBody.url).toBe("https://kspmas.ksyun.com/v1");
    expect(probeBody.apiKey).toBe("sk-test-12345678");

    expect(await screen.findByText("deepseek-v4.1-flash")).toBeVisible();
    expect(screen.getByRole("button", { name: "deepseek-v4.1-flash" })).toHaveAttribute("data-selected", "true");
    expect(screen.getByRole("button", { name: "BAAI/bge-m3" })).toHaveAttribute("data-selected", "false");
    // 探测出的协议徽章包含 Anthropic。
    expect(screen.getByText(/Anthropic·可用/)).toBeVisible();
  });

  it("saves the user-selected models with the recommended wireApi and endpoint", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (path, init) => {
      if (String(path).includes("model-endpoints:probe")) {
        return { ok: true, json: async () => PROBE_PAYLOAD } as Response;
      }
      if (String(path).startsWith("/api/v1/credentials/")) {
        return { ok: true, json: async () => ({ configured: true }) } as Response;
      }
      if (String(path).includes("model-profiles") && init?.method === "POST") {
        return { ok: true, json: async () => ({ resourceId: "m-new" }) } as Response;
      }
      return { ok: true, json: async () => ({ items: [] }) } as Response;
    });
    const onSaved = vi.fn();
    render(<ModelSetupForm onSaved={onSaved} />);

    await user.type(screen.getByLabelText(/^API Key/, { selector: "input" }), "sk-test-12345678");
    await user.click(screen.getByRole("button", { name: /重新探测/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: "glm-5.1" })).toBeVisible());

    // 用户手动多选 glm-5.1（预选的 deepseek 保持选中），手选优先。
    await user.click(screen.getByRole("button", { name: "glm-5.1" }));
    await user.click(screen.getByRole("button", { name: /创建模型（2 个）/ }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    const profilePosts = fetchMock.mock.calls.filter(([path, init]) => String(path).includes("catalog/model-profiles") && init?.method === "POST");
    expect(profilePosts).toHaveLength(2);
    const firstBody = JSON.parse(String(profilePosts[0]?.[1]?.body));
    expect(firstBody.spec.wireApi).toBe("anthropic");
    expect(firstBody.spec.endpointUrl).toBe("https://kspmas.ksyun.com/v1/messages");
    expect(firstBody.spec.credentialRef).toBe("env://MODEL_API_KEY");
    expect(firstBody.spec.parameters).not.toHaveProperty("thinking_mode");
    const credentialPut = fetchMock.mock.calls.find(([path, init]) => String(path).startsWith("/api/v1/credentials/") && init?.method === "PUT");
    expect(credentialPut).toBeDefined();
    expect(JSON.parse(String(credentialPut?.[1]?.body)).value).toBe("sk-test-12345678");
  });

  it("saves an explicit thinking selection and a 128K output budget for a Chat model", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ items: [], resourceId: "m-new" }) } as Response);
    const onSaved = vi.fn();
    render(<ModelSetupForm onSaved={onSaved} />);
    await user.type(screen.getByLabelText(/^模型 ID/, { selector: "input" }), "glm-5.3-flash");
    await user.click(screen.getByText("高级选项"));
    await user.clear(screen.getByLabelText("max_tokens"));
    await user.type(screen.getByLabelText("max_tokens"), "128000");
    await user.selectOptions(screen.getByLabelText("思考模式"), "enabled");
    await user.selectOptions(screen.getByLabelText("推理强度"), "max");
    await user.click(screen.getByLabelText("支持图像输入"));
    await user.click(screen.getByLabelText("展示模型返回的思考过程"));
    await user.click(screen.getByRole("button", { name: /创建模型/ }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    const call = fetchMock.mock.calls.find(([path, init]) => String(path).includes("catalog/model-profiles") && init?.method === "POST");
    expect(JSON.parse(String(call?.[1]?.body)).spec.parameters).toMatchObject({
      max_tokens: 128000, thinking_mode: "enabled", provider_thinking_display: true,
      reasoning_effort: "max",
    });
    expect(JSON.parse(String(call?.[1]?.body)).spec.metadata).toMatchObject({
      capabilities: { multimodal_input_image: true },
    });
  });

  it("does not silently drop an enabled selection on a different protocol", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async path => ({ ok: true, json: async () =>
      String(path).includes("model-endpoints:probe") ? PROBE_PAYLOAD : { items: [] },
    }) as Response);
    render(<ModelSetupForm onSaved={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /重新探测/ }));
    await screen.findByRole("button", { name: "deepseek-v4.1-flash" });
    await user.click(screen.getByText("高级选项"));
    await user.selectOptions(screen.getByLabelText("思考模式"), "enabled");
    await user.click(screen.getByRole("button", { name: /创建模型/ }));
    expect(await screen.findByText("当前连接协议不支持这个思考开关，请保留模型默认设置。")).toBeVisible();
    expect(fetchMock.mock.calls.some(([path, init]) => String(path).includes("catalog/model-profiles") && init?.method === "POST")).toBe(false);
  });
});
