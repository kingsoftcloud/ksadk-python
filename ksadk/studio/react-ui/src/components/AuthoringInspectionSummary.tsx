import { CodeViewer } from "./ui/CodeViewer";

interface Inspection {
  name?: string;
  displayName?: string;
  runtimeType?: string;
  projectPath?: string;
  entryPoint?: string;
  kind?: string;
  files?: unknown[];
}

export function AuthoringInspectionSummary({
  inspection,
  project = false,
}: {
  inspection: Inspection;
  project?: boolean;
}) {
  return (
    <div className="authoring-inspection-summary">
      <dl>
        <div>
          <dt>框架</dt>
          <dd>{inspection.runtimeType || "待确认"}</dd>
        </div>
        {project ? (
          <>
            <div>
              <dt>项目路径</dt>
              <dd>{inspection.projectPath || "."}</dd>
            </div>
            <div>
              <dt>入口</dt>
              <dd>{inspection.entryPoint || "使用框架默认入口"}</dd>
            </div>
          </>
        ) : (
          <>
            <div>
              <dt>文件格式</dt>
              <dd>{inspection.kind?.toUpperCase() || "Agent"}</dd>
            </div>
            {Array.isArray(inspection.files) && (
              <div>
                <dt>文件数量</dt>
                <dd>{inspection.files.length}</dd>
              </div>
            )}
          </>
        )}
      </dl>
      <details className="secondary-settings">
        <summary>查看完整检查结果</summary>
        <CodeViewer
          code={JSON.stringify(inspection, null, 2)}
          language="json"
          filename={
            project ? "project-inspection.json" : "agent-import-inspection.json"
          }
          showLineNumbers
        />
      </details>
    </div>
  );
}
