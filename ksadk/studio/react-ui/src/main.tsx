import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@kingsoftcloud/ksadk-web/styles";
import App from "./App.tsx";
import { initializeStudioSession } from "./api";
import { initializeStudioTheme } from "./studioTheme";
import "./index.css";
import "./kingdesign.css";
import "./studio-refinement.css";
import "./plugins.css";

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
      <App />
    </StrictMode>,
  );
  return () => root.unmount();
}

if (window.__STUDIO_DSH_BOOT__) {
  window.__STUDIO_APP__ = { mount };
  window.dispatchEvent(new Event('studio:app-ready'));
} else {
  void mount(document.getElementById("root")!);
}
