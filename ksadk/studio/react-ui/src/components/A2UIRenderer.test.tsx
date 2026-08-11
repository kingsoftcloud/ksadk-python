import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { A2UIRenderer } from "./A2UIRenderer";

describe("A2UIRenderer", () => {
  it("renders an approval surface and submits a structured action", async () => {
    const submit = vi.fn();
    render(
      <A2UIRenderer
        surface={{
          id: "surface-1",
          catalogId: "basic",
          roots: ["root"],
          dataModel: {},
          components: {
            root: { id: "root", component: "Card", title: "需要你的确认", children: ["approval"] },
            approval: { id: "approval", component: "ApprovalBar", summary: "写入配置", approve_label: "批准", deny_label: "拒绝" },
          },
          interaction: { id: "approval-1", kind: "approval", status: "pending", inputSchema: {} },
        }}
        onSubmit={submit}
      />,
    );

    expect(screen.getByText("需要你的确认")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "批准" }));
    expect(submit).toHaveBeenCalledWith("approval-1", "approve", {});
  });

  it("collects multi-select and custom text input values", async () => {
    const submit = vi.fn();
    render(
      <A2UIRenderer
        surface={{
          id: "surface-form",
          catalogId: "basic",
          roots: ["form"],
          dataModel: {},
          components: {
            form: { id: "form", component: "Form", title: "选择范围", submit_label: "继续", children: ["targets", "note"] },
            targets: { id: "targets", component: "CheckboxGroup", name: "targets", label: "目标", options: ["A", "B"] },
            note: { id: "note", component: "TextField", name: "note", label: "补充说明" },
          },
          interaction: { id: "interaction-1", kind: "form", status: "pending", inputSchema: {} },
        }}
        onSubmit={submit}
      />,
    );

    await userEvent.click(screen.getByLabelText("A"));
    await userEvent.type(screen.getByLabelText("补充说明"), "仅检查");
    await userEvent.click(screen.getByRole("button", { name: "继续" }));
    expect(submit).toHaveBeenCalledWith("interaction-1", "submit", {
      targets: ["A"],
      note: "仅检查",
    });
  });

  it("renders quiet described choices and submits a free-form other answer", async () => {
    const submit = vi.fn();
    render(
      <A2UIRenderer
        surface={{
          id: "surface-question",
          catalogId: "basic",
          roots: ["form"],
          dataModel: {},
          components: {
            form: { id: "form", component: "Form", title: "需要你的反馈", submit_label: "提交", children: ["scope"] },
            scope: {
              id: "scope",
              component: "MultipleChoice",
              name: "scope",
              label: "检查范围",
              description: "选择一项，也可以直接输入。",
              allow_other: true,
              options: [
                { label: "前端", value: "frontend", description: "只检查 React。" },
                { label: "全栈", value: "fullstack", description: "同时检查服务端。" },
              ],
            },
          },
          interaction: { id: "question-1", kind: "form", status: "pending", inputSchema: {} },
        }}
        onSubmit={submit}
      />,
    );

    expect(screen.getByText("只检查 React。")).toBeVisible();
    await userEvent.type(screen.getByLabelText("检查范围自定义输入"), "只检查协议层");
    await userEvent.click(screen.getByRole("button", { name: /提交/ }));

    expect(submit).toHaveBeenCalledWith("question-1", "submit", {
      scope: "只检查协议层",
    });
  });

  it("applies streamed data-model updates without overwriting a field the user edited", async () => {
    const submit = vi.fn();
    const surface = {
      id: "surface-streamed",
      catalogId: "basic",
      roots: ["form"],
      dataModel: { summary: "等待更新", note: "服务端默认" },
      components: {
        form: { id: "form", component: "Form", children: ["summary", "note"] },
        summary: { id: "summary", component: "TextField", name: "summary", label: "摘要" },
        note: { id: "note", component: "TextField", name: "note", label: "补充" },
      },
      interaction: { id: "interaction-streamed", kind: "form", status: "pending" as const, inputSchema: {} },
    };
    const view = render(<A2UIRenderer surface={surface} onSubmit={submit} />);
    await userEvent.clear(screen.getByLabelText("补充"));
    await userEvent.type(screen.getByLabelText("补充"), "用户输入");

    view.rerender(
      <A2UIRenderer
        surface={{ ...surface, dataModel: { summary: "流式更新完成", note: "服务端覆盖" } }}
        onSubmit={submit}
      />,
    );

    expect(screen.getByLabelText("摘要")).toHaveValue("流式更新完成");
    expect(screen.getByLabelText("补充")).toHaveValue("用户输入");
  });
});
