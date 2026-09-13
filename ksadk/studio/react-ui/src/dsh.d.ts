export {};
declare global {
  interface Window {
    studioNative?: {
      chooseWorkspace?(): Promise<string | null>;
      openWorkspace?(): Promise<{ path: string } | null>;
      onWorkspaceOpened?(callback: (workspace: { path: string }) => void): () => void;
    };
    __STUDIO_DSH_BOOT__?: boolean;
    __STUDIO_APP__?: { mount(container: HTMLElement): Promise<() => void>; mountWorkspace(componentId: string, container: HTMLElement, props?: Record<string, unknown>): Promise<() => void> };
    __STUDIO_DSH__?: {
      workspacePages?(): { id: string; label: string; pluginId?: string; order?: number }[];
      subscribeWorkspace?(listener: () => void): () => void;
      attachWorkspace?(id: string, container: HTMLElement, props?: Record<string, unknown>): () => void;
      sections(): { id: string; label: string; pluginId?: string }[];
      subscribe(listener: () => void): () => void;
      attach(id: string, container: HTMLElement, close: () => void): () => void;
    };
  }
}
