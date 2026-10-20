import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { ProjectInstructionSettings } from "./ProjectInstructionSettings";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);
beforeEach(() => { fetchMock.mockReset(); });

it("loads workspace consent and saves the explicit toggle without selecting a principal", async () => {
  fetchMock.mockImplementation(async (_path, init) => ({
    ok: true,
    json: async () => ({ trusted: init?.method === "PUT" ? JSON.parse(String(init.body)).trusted : false }),
  }) as Response);
  render(<ProjectInstructionSettings />);
  const checkbox = screen.getByRole("checkbox");
  await waitFor(() => expect(checkbox).toBeEnabled());
  expect(checkbox).not.toBeChecked();
  await userEvent.click(checkbox);
  await waitFor(() => expect(checkbox).toBeChecked());
  const mutation = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
  expect(mutation?.[0]).toBe("/api/v1/workspace/project-instructions");
  expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({ trusted: true });
  await userEvent.click(checkbox);
  await waitFor(() => expect(checkbox).not.toBeChecked());
});

it("does not grant consent when the host state is unavailable", async () => {
  fetchMock.mockResolvedValue({ ok: false } as Response);
  render(<ProjectInstructionSettings />);
  await screen.findByRole("alert");
  expect(screen.getByRole("checkbox")).toBeDisabled();
  expect(screen.getByRole("checkbox")).not.toBeChecked();
});
