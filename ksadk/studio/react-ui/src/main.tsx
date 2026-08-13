import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.tsx";
import { initializeStudioSession } from "./api";
import { initializeStudioTheme } from "./studioTheme";
import "./index.css";

async function main() {
  initializeStudioTheme();
  try {
    await initializeStudioSession();
  } catch {
    // App 会保留未连接状态；用户可使用新的 CLI 启动链接重新建立会话。
  }

  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void main();
