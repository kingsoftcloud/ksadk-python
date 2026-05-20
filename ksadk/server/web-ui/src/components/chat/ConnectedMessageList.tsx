import React, { useEffect, useRef, useCallback } from 'react';
import { useUIStore } from '../../stores/ui.js';
import { useStreamingStore } from '../../stores/streaming.js';
import { useMessageStore } from '../../stores/message.js';
import { ChatMessageList } from './ChatMessageList';
import { AttachmentPreview } from './AttachmentPreview';
import type { Message, MessageAttachment } from './types';

type ConnectedMessageListProps = {
  agentName: string;
  isMobile: boolean;
  onDeleteFeedback: (message: Message) => void;
  onSubmitFeedback: (options: { message: Message; rating: 'up' | 'down'; comment?: string }) => void;
  onRespondToApproval: (options: { approvalRequestId: string; approve: boolean; previousResponseId?: string }) => void;
  submitDraft: (text: string, attachments: File[], responsesInput?: unknown, previousResponseId?: string) => Promise<void>;
  composerMaxHeight: number;
};

export function ConnectedMessageList({
  agentName,
  isMobile,
  onDeleteFeedback,
  onSubmitFeedback,
  onRespondToApproval,
  submitDraft,
  composerMaxHeight,
}: ConnectedMessageListProps) {
  const messages = useMessageStore(s => s.messages);
  const isStreaming = useStreamingStore(s => s.isStreaming);
  const previewAttachment = useUIStore(s => s.previewAttachment);
  const previewImageSize = useUIStore(s => s.previewImageSize);
  const scrollRef = useRef<HTMLDivElement>(null);

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
        messages={messages}
        onDeleteFeedback={onDeleteFeedback}
        onOpenAttachmentPreview={openAttachmentPreview}
        onRespondToApproval={onRespondToApproval}
        onSubmitFeedback={onSubmitFeedback}
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
