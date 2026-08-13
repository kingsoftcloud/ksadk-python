import { CloudUpload } from "lucide-react";

/** 一期部署视图保留入口并展示空态指引。 */
export function DeploymentsPage({ onCreate }: { onCreate: () => void }) {
  void onCreate;
  return (
    <div className="page-container" data-layout="data" data-scroll-mode="data">
      <header className="page-header">
        <div><h1>部署</h1><p>把本地生成的同一 AgentBundle 发布至云端运行环境。</p></div>
      </header>
      <div className="data-page-body">
        <div className="empty-page-state">
          <span className="empty-icon"><CloudUpload size={24} /></span>
          <h2>选择已构建的 AgentBundle</h2>
          <p>从 Agent 详情或构建页面发起部署，云端仅注入环境绑定，不重新构建。</p>
        </div>
      </div>
    </div>
  );
}
