import { lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";

// Trusted host components are loaded only after a plugin mounts its registered slot.
// This registry is separate from the frozen Agent renderer catalog.
const components = {
  teams: lazy(() =>
    import("../pages/TeamsPage").then((module) => ({
      default: module.TeamsPage,
    })),
  ),
};
export async function mountWorkspace(
  componentId: string,
  container: HTMLElement,
  props: Record<string, unknown> = {},
) {
  const Component = components[componentId as keyof typeof components];
  if (!Component) throw new Error(`未知工作区组件：${componentId}`);
  const root = createRoot(container);
  root.render(
    <div className="studio-workspace-component-root">
    <Suspense
      fallback={
        <p className="studio-plugin-loading" role="status">
          正在打开工作区…
        </p>
      }
    >
      <Component {...props} />
    </Suspense>
    </div>,
  );
  let disposed = false;
  return () => {
    if (disposed) return;
    disposed = true;
    // DSH calls disposal from its own React commit. Finish that commit before
    // unmounting the independent host root; its scopes close in the same tick.
    queueMicrotask(() => root.unmount());
  };
}
