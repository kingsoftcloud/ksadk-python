import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { TaskPlan, type ToolDisplay } from "./ToolExecutionCard";

const items: NonNullable<ToolDisplay["todos"]> = Array.from({ length: 6 }, (_, index) => ({
  id: String(index), content: `Check ${index}`, priority: "high",
  status: index < 2 ? "completed" : index === 2 ? "in_progress" : "pending",
}));

it("preserves 2/6 on termination without claiming the unfinished work is still running", async () => {
  const { container, rerender } = render(<TaskPlan items={items} runState={{ status: "running" }} />);
  const plan = container.querySelector("details")!;
  expect(plan).toHaveTextContent("进程2/6");
  await userEvent.setup().click(plan.querySelector("summary")!);
  expect(plan).toHaveTextContent("进行中");
  rerender(<TaskPlan items={items} runState={{ status: "completed", outcome: "unverified" }} />);
  expect(plan).toHaveTextContent("进程2/6");
  expect(plan).toHaveTextContent("该轮已结束，仍有 4 项未标记完成");
  expect(plan).toHaveTextContent("验收尚未确认");
  expect(plan).toHaveTextContent("结束前进行中");
  expect(plan.querySelectorAll('li[data-status="completed"]')).toHaveLength(2);
  expect(plan.querySelector("progress")).toHaveAttribute("value", "2");
});

it("does not promote an all-completed checklist to verified acceptance", () => {
  const { container } = render(<TaskPlan items={items.map(item => ({ ...item, status: "completed" }))}
    runState={{ status: "completed", outcome: "unverified" }} />);
  expect(container).toHaveTextContent("进程6/6");
  expect(container).toHaveTextContent("验收尚未确认");
  expect(container).not.toHaveTextContent("验收通过");
});

it("does not infer a terminal or accepted state when host status is unavailable", () => {
  const { container } = render(<TaskPlan items={items} />);
  expect(container).not.toHaveTextContent("该轮已结束");
  expect(container).not.toHaveTextContent("验收通过");
});
