export {};
declare global {
  interface Window {
    __STUDIO_DSH_BOOT__?: boolean;
    __STUDIO_APP__?: { mount(container: HTMLElement): Promise<() => void> };
    __STUDIO_DSH__?: {
      sections(): { id: string; label: string; pluginId?: string }[];
      subscribe(listener: () => void): () => void;
      attach(id: string, container: HTMLElement, close: () => void): () => void;
    };
  }
}
