import { useEffect, useMemo, useRef } from 'react';
import { useUIStore } from '../../stores/ui.js';
import { useStreamingStore } from '../../stores/streaming.js';
import { useMessageStore } from '../../stores/message.js';
import { useSessionStore } from '../../stores/session.js';
import { useModelStore } from '../../stores/model.js';
import { ChatMessageList } from './ChatMessageList';
import { AttachmentPreview } from './AttachmentPreview';
import { buildComposerContextIndicator } from '../../utils/context.js';
import type { Message, MessageAttachment } from './types';

type ConnectedMessageListProps = {
  agentName: string;
  isMobile: boolean;
  onDeleteFeedback: (message: Message) => void;
  onSubmitFeedback: (options: { message: Message; rating: 'up' | 'down'; comment?: string }) => void;
  onRespondToApproval: (options: { approvalRequestId: string; approve: boolean; previousResponseId?: string }) => void;
  onStopGeneration?: () => void;
  onCancelRemote?: () => void;
};

export function ConnectedMessageList({
  agentName,
  isMobile,
  onDeleteFeedback,
  onSubmitFeedback,
  onRespondToApproval,
  onStopGeneration,
  onCancelRemote,
}: ConnectedMessageListProps) {
  const messages = useMessageStore(s => s.messages);
  const currentSessionId = useSessionStore(s => s.currentSessionId);
  const isStreaming = useStreamingStore(s => Boolean(s.getSessionActivity(currentSessionId) && s.isSessionStreaming(currentSessionId)));
  const activity = useStreamingStore(s => s.getSessionActivity(currentSessionId));
  const input = useUIStore(s => s.input);
  const availableModels = useModelStore(s => s.availableModels);
  const selectedModel = useModelStore(s => s.selectedModel);
  const previewAttachment = useUIStore(s => s.previewAttachment);
  const previewImageSize = useUIStore(s => s.previewImageSize);
  const scrollRef = useRef<HTMLDivElement>(null);
  const selectedModelMetadata = useMemo(
    () => availableModels.find((model) => model.id === selectedModel) || null,
    [availableModels, selectedModel],
  );
  const contextIndicator = useMemo(
    () =>
      buildComposerContextIndicator({
        messages,
        draftInput: input,
        selectedModel: selectedModelMetadata,
      }),
    [input, messages, selectedModelMetadata],
  );

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, isStreaming]);

  const openAttachmentPreview = (attachment: MessageAttachment) => {
    useUIStore.getState().setPreviewAttachment(attachment);
    useUIStore.getState().setPreviewImageSize(null);
  };

  const closeAttachmentPreview = () => {
    useUIStore.getState().setPreviewAttachment(null);
    useUIStore.getState().setPreviewImageSize(null);
  };

  return (
    <>
      <ChatMessageList
        agentName={agentName}
        isMobile={isMobile}
        isStreaming={isStreaming}
        activity={activity}
        contextIndicator={contextIndicator}
        messages={messages}
        onDeleteFeedback={onDeleteFeedback}
        onOpenAttachmentPreview={openAttachmentPreview}
        onRespondToApproval={onRespondToApproval}
        onSubmitFeedback={onSubmitFeedback}
        onStopGeneration={onStopGeneration}
        onCancelRemote={onCancelRemote}
        scrollRef={scrollRef}
      />
      <AttachmentPreview
        attachment={previewAttachment}
        isMobile={isMobile}
        previewImageSize={previewImageSize}
        onClose={closeAttachmentPreview}
        onImageLoad={(size) => useUIStore.getState().setPreviewImageSize(size)}
      />
    </>
  );
}
