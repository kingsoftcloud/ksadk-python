import { useEffect, useState } from "react";
import { apiFetch } from "../api";
import { showToast } from "./Toast";

/** Workspace-local consent; the server fixes the authenticated local principal. */
export function ProjectInstructionSettings() {
  const [trusted, setTrusted] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    apiFetch("/api/v1/workspace/project-instructions").then(async response => {
      if (!response.ok) throw new Error("unavailable");
      const state = await response.json();
      if (active) setTrusted(state.trusted === true);
    }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, []);

  async function update(value: boolean) {
    setBusy(true);
    try {
      const response = await apiFetch("/api/v1/workspace/project-instructions", {
        method: "PUT", body: JSON.stringify({ trusted: value }),
      });
      if (!response.ok) throw new Error("unavailable");
      const state = await response.json();
      setTrusted(state.trusted === true);
    } catch {
      showToast("项目指令设置保存失败", "", "error");
    } finally { setBusy(false); }
  }

  return <div className="studio-form-field">
    <label className="checkbox-row">
      <input type="checkbox" checked={trusted === true} disabled={trusted === null || busy}
        onChange={event => void update(event.target.checked)} />
      <span><strong>读取当前工作区的项目指令</strong>
        <small>允许 Native 编程智能体读取 AGENTS.md 和目录指令；仅对当前工作区与用户生效。
          此设置不扩大工具权限，关闭后在下一次上下文收集时生效。</small>
        {failed && <small role="alert">项目指令设置暂时无法读取</small>}
      </span>
    </label>
  </div>;
}
