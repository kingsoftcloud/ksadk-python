import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { DshPluginWorkspace } from "./DshPluginWorkspace";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);
beforeEach(() => fetchMock.mockReset());

it("opens the official UI inside Studio without a popup", async () => {
  fetchMock.mockResolvedValue({ ok: true, json: async () => ({ browserUrl: "http://127.0.0.1:43123/?token=test" }) } as Response);
  const open = vi.spyOn(window, "open");
  const onBack = vi.fn();
  render(<DshPluginWorkspace onBack={onBack}/>);
  expect(screen.getByRole("status")).toHaveTextContent("正在打开插件工作台");
  const frame = await screen.findByTitle("DSH 插件工作台");
  expect(frame).toHaveAttribute("src", "http://127.0.0.1:43123/?token=test");
  fireEvent.load(frame);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(open).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "返回插件列表" }));
  expect(onBack).toHaveBeenCalledOnce();
  open.mockRestore();
});

it("keeps startup errors visible and lets the user retry", async () => {
  fetchMock.mockResolvedValueOnce({ ok: false, json: async () => ({ error: { message: "DSH capability host 当前不可用" } }) } as Response);
  render(<DshPluginWorkspace onBack={() => {}}/>);
  expect(await screen.findByRole("alert")).toHaveTextContent("DSH capability host 当前不可用");
  expect(screen.queryByTitle("DSH 插件工作台")).not.toBeInTheDocument();
  fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({ browserUrl: "http://127.0.0.1:43124/?token=test" }) } as Response);
  await userEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByTitle("DSH 插件工作台")).toBeInTheDocument();
});

it("does not navigate an embedded browser to an unexpected origin", async () => {
  fetchMock.mockResolvedValue({ ok: true, json: async () => ({ browserUrl: "https://example.com/?token=test" }) } as Response);
  render(<DshPluginWorkspace onBack={() => {}}/>);
  expect(await screen.findByRole("alert")).toHaveTextContent("插件工作台地址无效");
  expect(screen.queryByTitle("DSH 插件工作台")).not.toBeInTheDocument();
});
