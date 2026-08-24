import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { RuntimeResourcesPage } from "./RuntimeResourcesPage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

const mockedFetch = vi.mocked(apiFetch);

describe("RuntimeResourcesPage", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedFetch.mockImplementation(async input => {
      const url = String(input);
      if (url.startsWith("/api/v1/catalog/resources")) {
        return {
          ok: true,
          json: async () => ({
            items: Array.from({ length: 8 }, (_, index) => ({
              resourceId: `tool-${index + 1}`,
              kind: "tool",
              name: `tool-${index + 1}`,
              displayName: `Tool ${index + 1}`,
              version: "1.0.0",
              status: index === 7 ? "unhealthy" : "ready",
              source: "local",
            })),
          }),
        } as Response;
      }
      if (url.startsWith("/api/v1/catalog/models")) {
        return { ok: true, json: async () => ({ items: [] }) } as Response;
      }
      if (url.startsWith("/api/v1/runs")) {
        return { ok: true, json: async () => ({ items: [] }) } as Response;
      }
      if (url.startsWith("/api/v1/system/bootstrap")) {
        return { ok: true, json: async () => ({ workspace: { path: "/workspace/demo" } }) } as Response;
      }
      return { ok: true, json: async () => ({ total: 0, buckets: [] }) } as Response;
    });
  });

  it("keeps the overview compact, shows problems first, and links to the full catalog", async () => {
    const user = userEvent.setup();
    const onOpenResources = vi.fn();
    render(<RuntimeResourcesPage refreshTick={0} onOpenResources={onOpenResources} />);

    expect(await screen.findByText("Tool 8")).toBeVisible();
    expect(screen.getByText("异常")).toBeVisible();
    expect(screen.getByText("8 个已发现")).toBeVisible();
    expect(screen.getAllByText(/^Tool \d+$/)).toHaveLength(5);
    expect(screen.queryByText("Tool 5")).not.toBeInTheDocument();
    expect(screen.getByText("趋势数据不足")).toBeVisible();

    const buttons = screen.getAllByRole("button", { name: /查看全部/ });
    await user.click(buttons[1]);
    expect(onOpenResources).toHaveBeenCalledWith("tool");
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledWith("/api/v1/catalog/resources?limit=200"));
  });

  it("renders trace buckets with the runtime trend chart classes", async () => {
    mockedFetch.mockImplementation(async input => {
      const url = String(input);
      if (url.startsWith("/api/v1/catalog/resources")) {
        return { ok: true, json: async () => ({ items: [] }) } as Response;
      }
      if (url.startsWith("/api/v1/catalog/models")) {
        return { ok: true, json: async () => ({ items: [] }) } as Response;
      }
      if (url.startsWith("/api/v1/runs")) {
        return { ok: true, json: async () => ({ items: [] }) } as Response;
      }
      if (url.startsWith("/api/v1/system/bootstrap")) {
        return { ok: true, json: async () => ({ workspace: { path: "/workspace/demo" } }) } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          total: 3,
          buckets: [
            { startedAt: "2026-08-19T08:00:00Z", runs: 1, completed: 1 },
            { startedAt: "2026-08-19T09:00:00Z", runs: 2, completed: 2 },
          ],
        }),
      } as Response;
    });

    const { container } = render(<RuntimeResourcesPage refreshTick={0} onOpenResources={vi.fn()} />);

    await screen.findByText("峰值 2");
    expect(container.querySelector(".runtime-trend-bars")).toBeInTheDocument();
    expect(container.querySelectorAll(".runtime-trend-bar")).toHaveLength(2);
    expect(container.querySelectorAll('.runtime-trend-bar[data-peak="true"]')).toHaveLength(1);
  });
});
