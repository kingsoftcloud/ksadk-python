import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "@kingsoftcloud/ksadk-web/styles";
import { mountWorkspace } from "./plugins/workspaceRegistry";
import App from "./App.tsx";
import { initializeStudioSession } from "./api";
import { initializeStudioTheme } from "./studioTheme";
import "./index.css";
import "./kingdesign.css";
import "./studio-refinement.css";
import "./plugins.css";
import "./layout-simplification.css";
import "./mobile-resource-lists.css";
import "./teams.css";
import "@kingsoftcloud/ksadk-web/teams/styles";

function WorkspaceApp() {
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const opened = () => {
      // A Core document owns a workspace-specific plugin context. Return to
      // the lightweight shell when leaving that context; normal workspace
      // navigation only remounts the local UI, never the Python process.
      if (window.__STUDIO_DSH__) {
        window.location.assign("/");
        return;
      }
      window.history.replaceState(null, "", "/#/agents");
      setGeneration(value => value + 1);
    };
    const unsubscribe = window.studioNative?.onWorkspaceOpened?.(opened);
    window.addEventListener("studio:directory-opened", opened);
    return () => {
      unsubscribe?.();
      window.removeEventListener("studio:directory-opened", opened);
    };
  }, []);
  return <App key={generation} />;
}

async function mount(container: HTMLElement) {
  initializeStudioTheme();
  document.title = 'AgentKit Studio';
  try {
    await initializeStudioSession();
  } catch {
    // App 会保留未连接状态；用户可使用新的 CLI 启动链接重新建立会话。
  }

  const root = createRoot(container);
  root.render(
    <StrictMode>
      <WorkspaceApp />
    </StrictMode>,
  );
  return () => root.unmount();
}

window.__STUDIO_APP__ = { mount, mountWorkspace };
if (window.__STUDIO_DSH_BOOT__) {
  window.dispatchEvent(new Event('studio:app-ready'));
} else {
  void mount(document.getElementById("root")!);
}
