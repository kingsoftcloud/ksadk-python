import { useEffect } from 'react';
import { useBootstrapStore } from '../stores/bootstrap.js';
import { useModelStore } from '../stores/model.js';
import { useUIStore } from '../stores/ui.js';
import { getAgentUiBootstrap } from '../api/bootstrap.js';
import { listAgentModels } from '../api/model.js';
import { normalizeCapabilities, isHostedChatEnabled } from '../utils/capabilities.js';
import { readPersistedSessionId } from '../utils/session.js';
import type { UiCapabilities } from '../types/capabilities.js';
import type { BootstrapModel, BootstrapWorkspaceFiles } from '../types/bootstrap.js';
import type { RuntimeApiFormat } from '../types/api.js';

function normalizeApiFormats(value: unknown): RuntimeApiFormat[] {
  if (!Array.isArray(value)) {
    return ['responses', 'chat_completions'];
  }
  const formats = value.filter(
    (item): item is RuntimeApiFormat => item === 'responses' || item === 'chat_completions',
  );
  return formats.length > 0 ? formats : ['responses', 'chat_completions'];
}

async function fetchModels(targetAgentId: string) {
  try {
    const data = await listAgentModels(targetAgentId);
    const models = (data as Record<string, unknown>)?.Models;
    if (Array.isArray(models)) {
      useModelStore.getState().upsertModels(models as import('../components/chat/types.js').ModelCatalogItem[]);
    }
    const dataRecord = data as Record<string, unknown>;
    if ((dataRecord as Record<string, unknown>)?.Current) {
      useModelStore.getState().setSelectedModel(String((dataRecord as Record<string, unknown>).Current));
    }
    if ((dataRecord as Record<string, unknown>)?.Source) {
      useModelStore.getState().setModelSource(String((dataRecord as Record<string, unknown>).Source));
    }
  } catch (error) {
    console.error('Failed to fetch models:', error);
  } finally {
    useModelStore.getState().setModelCatalogLoaded(true);
  }
}

type SessionCallbacks = {
  fetchSessions: (agentId: string, preferredSessionId: string | null) => Promise<void>;
};

export function useBootstrap(sessionCallbacks: SessionCallbacks) {
  useEffect(() => {
    void (async () => {
      try {
        const data = await getAgentUiBootstrap();
        const dataRecord = data as Record<string, unknown>;
        const agentRecord = dataRecord?.Agent as Record<string, unknown> | undefined;
        const bootstrapAgentId = String(agentRecord?.AgentId || 'default-agent');
        useBootstrapStore.getState().setAgentId(bootstrapAgentId);
        if (agentRecord?.Name) {
          const name = String(agentRecord.Name);
          useBootstrapStore.getState().setAgentName(name);
          document.title = name;
        }
        const normalizedFramework = String(agentRecord?.Framework || '').trim().toLowerCase();
        useBootstrapStore.getState().setAgentFramework(normalizedFramework);
        const normalizedCapabilities = normalizeCapabilities(data) as UiCapabilities;
        useBootstrapStore.getState().setCapabilities(normalizedCapabilities);
        useBootstrapStore.getState().setApiFormats(normalizeApiFormats(normalizedCapabilities.HostedChat.ApiFormats));
        useBootstrapStore.getState().setAccessMode(String(dataRecord?.AccessMode || 'Owner'));

        const bootstrapWorkspaceFiles =
          normalizedCapabilities.WorkspaceFiles && (dataRecord as Record<string, unknown>)?.WorkspaceFiles
            ? ((dataRecord as Record<string, unknown>).WorkspaceFiles as BootstrapWorkspaceFiles)
            : null;
        useBootstrapStore.getState().setWorkspaceFiles(
          bootstrapWorkspaceFiles as Record<string, unknown> | null,
        );
        if (!bootstrapWorkspaceFiles) {
          useUIStore.getState().setWorkspacePanelOpen(false);
        }

        if (isHostedChatEnabled(normalizedCapabilities)) {
          void sessionCallbacks.fetchSessions(
            bootstrapAgentId,
            readPersistedSessionId(bootstrapAgentId),
          );
        }

        const bootstrapModel: BootstrapModel | undefined = dataRecord?.Model as BootstrapModel | undefined;
        if (bootstrapModel?.id) {
          useModelStore.getState().setSelectedModel(bootstrapModel.id);
          useModelStore.getState().upsertModels([bootstrapModel]);
          useModelStore.getState().setModelSource(bootstrapModel.source || '');
        }
        void fetchModels(bootstrapAgentId);
      } catch (error) {
        console.error('Failed to fetch bootstrap:', error);
        useBootstrapStore.getState().setCapabilities(
          normalizeCapabilities({
            Data: {
              Agent: { Framework: '' },
              ApiFormats: ['responses', 'chat_completions'],
              Capabilities: {},
            },
          }) as UiCapabilities,
        );
        void sessionCallbacks.fetchSessions('default-agent', readPersistedSessionId('default-agent'));
        void fetchModels('default-agent');
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
