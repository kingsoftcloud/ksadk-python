import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ConversationFind } from "./ConversationFind";
import type { HistorySearchResult } from "@kingsoftcloud/ksadk-web/conversation";

const result: HistorySearchResult = { matches: [{ messageId: "older-17", role: "model", excerpt: "旧消息中的验收目标" }],
  searchedMessages: 2000, matchedMessages: 1, complete: true };

describe("current conversation find", () => {
  it("shows complete search results and navigates by message identity", async () => {
    const search = vi.fn().mockResolvedValue(result);
    const onReveal = vi.fn();
    render(<ConversationFind search={search} onReveal={onReveal} onClose={vi.fn()} />);
    const input = screen.getByRole("searchbox", { name: "查找当前会话正文" });
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: "验收" } });
    await screen.findByText("已查找全部历史，1 条消息匹配");
    fireEvent.click(screen.getByRole("button", { name: /旧消息中的验收目标/ }));
    expect(onReveal).toHaveBeenCalledWith("older-17");
  });

  it("aborts a superseded query and rejects its late progress and result", async () => {
    let oldSignal: AbortSignal | undefined;
    let oldProgress: ((result: HistorySearchResult) => void) | undefined;
    let finish: ((result: HistorySearchResult) => void) | undefined;
    const search = vi.fn((query: string, signal: AbortSignal, progress: (value: HistorySearchResult) => void) => {
      if (query === "旧") {
        oldSignal = signal; oldProgress = progress;
        return new Promise<HistorySearchResult>(resolve => { finish = resolve; });
      }
      return Promise.resolve({ ...result, matches: [], matchedMessages: 0 });
    });
    render(<ConversationFind search={search} onReveal={vi.fn()} onClose={vi.fn()} />);
    const input = screen.getByRole("searchbox");
    fireEvent.change(input, { target: { value: "旧" } });
    await waitFor(() => expect(oldSignal).toBeDefined());
    fireEvent.change(input, { target: { value: "新" } });
    expect(oldSignal?.aborted).toBe(true);
    oldProgress?.(result); finish?.(result);
    await screen.findByText("已查找全部历史，0 条消息匹配");
    expect(screen.queryByText(result.matches[0].excerpt)).not.toBeInTheDocument();
  });

  it("reports partial history on a read error and permits retry", async () => {
    const search = vi.fn().mockRejectedValueOnce(new Error("历史服务不可用"))
      .mockResolvedValueOnce(result);
    render(<ConversationFind search={search} onReveal={vi.fn()} onClose={vi.fn()} />);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "验收" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("历史服务不可用");
    expect(screen.queryByText(/已查找全部历史/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新查找" }));
    await screen.findByText("已查找全部历史，1 条消息匹配");
  });
});
