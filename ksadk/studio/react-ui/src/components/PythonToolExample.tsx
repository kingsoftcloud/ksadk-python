import { useId, useState } from "react";
import { BookOpen, ChevronDown } from "lucide-react";
import { CodeViewer } from "./ui/CodeViewer";

const PYTHON_TOOL_EXAMPLE = `def query_order(
    order_id: str,
    include_history: bool = False,
) -> dict[str, object]:
    """查询订单状态，并按需返回流转记录。"""
    result: dict[str, object] = {
        "order_id": order_id,
        "status": "processing",
    }
    if include_history:
        result["history"] = ["created", "paid"]
    return result
`;

export function PythonToolExample() {
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();

  return (
    <section className="python-tool-example" aria-label="Python Tool 编写帮助">
      <button
        className="python-tool-example-trigger"
        type="button"
        aria-label={expanded ? "收起编写示例" : "查看编写示例"}
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={() => setExpanded(value => !value)}
      >
        <BookOpen size={17} aria-hidden="true" />
        <span>
          <strong>Python Tool 怎么写？</strong>
          <small>查看可复制的函数样例与约定</small>
        </span>
        <ChevronDown className="python-tool-example-chevron" size={17} aria-hidden="true" />
      </button>
      {expanded && (
        <div id={panelId} className="python-tool-example-panel" role="region" aria-label="Python Tool 编写示例">
          <CodeViewer
            code={PYTHON_TOOL_EXAMPLE}
            language="python"
            filename="tool_example.py"
            wrap
            showLineNumbers
          />
          <ul className="python-tool-example-rules">
            <li>公开函数需定义在模块顶层，同步与异步函数均可。</li>
            <li>建议提供类型注解和 docstring，方便生成清晰的 Tool Contract。</li>
            <li>返回值应可 JSON 序列化；模块导入阶段不要执行有副作用的逻辑。</li>
          </ul>
        </div>
      )}
    </section>
  );
}
