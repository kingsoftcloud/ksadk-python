import type { ModelCatalogItem } from '../components/chat/types.js';
import type { WorkspaceFilesCapability } from '../components/chat/types.js';

export type BootstrapModel = ModelCatalogItem & {
  source?: string;
};

export type BootstrapWorkspaceFiles = WorkspaceFilesCapability;