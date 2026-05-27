import React, { useEffect, useMemo, useRef, useCallback } from 'react';
import { useUIStore } from '../../stores/ui.js';
import { useStreamingStore } from '../../stores/streaming.js';
import { useMessageStore } from '../../stores/message.js';
import { useModelStore } from '../../stores/model.js';
import { useSessionStore } from '../../stores/session.js';
import { ChatComposer } from './ChatComposer';
import { mergeAttachmentFiles, extractClipboardFiles } from '../../utils/attachment.js';
import { buildComposerContextIndicator } from '../../utils/context.js';

type ConnectedComposerProps = {
  composerMaxHeight: number;
  submitDraft: (text: string, attachments: File[]) => Promise<void>;
  stopGeneration: () => void;
  cancelRemote?: () => void;
  isMobile: boolean;
};

export function ConnectedComposer({
  composerMaxHeight,
  submitDraft,
  stopGeneration,
  cancelRemote,
  isMobile,
}: ConnectedComposerProps) {
  const input = useUIStore(s => s.input);
  const attachments = useUIStore(s => s.attachments);
  const currentSessionId = useSessionStore(s => s.currentSessionId);
  const isStreaming = useStreamingStore(s => Boolean(s.getSessionActivity(currentSessionId) && s.isSessionStreaming(currentSessionId)));
  const queuedDrafts = useUIStore(s => s.queuedDrafts);
  const messages = useMessageStore(s => s.messages);
  const availableModels = useModelStore(s => s.availableModels);
  const selectedModel = useModelStore(s => s.selectedModel);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const selectedModelMetadata = useMemo(
    () => availableModels.find((model) => model.id === selectedModel) || null,
    [availableModels, selectedModel],
  );
  const composerContextIndicator = useMemo(
    () =>
      buildComposerContextIndicator({
        messages,
        draftInput: input,
        selectedModel: selectedModelMetadata,
      }),
    [input, messages, selectedModelMetadata],
  );

  const handleSubmit = useCallback((draftText: string, draftAttachments: File[]) => {
    if (!draftText && draftAttachments.length === 0) return;
    useUIStore.getState().setInput('');
    useUIStore.getState().setAttachments([]);
    void submitDraft(draftText, draftAttachments);
  }, [submitDraft]);

  useEffect(() => {
    if (!textareaRef.current) return;
    textareaRef.current.style.height = 'auto';
    textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, composerMaxHeight)}px`;
  }, [input, composerMaxHeight]);

  const handleInputChange = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    useUIStore.getState().setInput(event.target.value);
    event.target.style.height = 'auto';
    event.target.style.height = `${Math.min(event.target.scrollHeight, composerMaxHeight)}px`;
  };

  const appendAttachments = (incoming: File[]) => {
    if (!incoming.length) return;
    useUIStore.getState().setAttachments((prev: File[]) => mergeAttachmentFiles(prev, incoming));
  };

  const handleComposerPaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const pastedFiles = extractClipboardFiles(event);
    if (!pastedFiles.length) return;
    event.preventDefault();
    event.stopPropagation();
    appendAttachments(pastedFiles);
  };

  return (
    <ChatComposer
      attachments={attachments}
      composerContextIndicator={composerContextIndicator}
      composerMaxHeight={composerMaxHeight}
      fileInputRef={fileInputRef}
      input={input}
      isMobile={isMobile}
      isStreaming={isStreaming}
      queuedDrafts={queuedDrafts}
      onAppendAttachments={appendAttachments}
      onInputChange={handleInputChange}
      onPaste={handleComposerPaste}
      onRemoveAttachment={(index) =>
        useUIStore.getState().setAttachments((prev: File[]) =>
          prev.filter((_, attachmentIndex) => attachmentIndex !== index),
        )
      }
      onStopGeneration={stopGeneration}
      onCancelRemote={cancelRemote}
      onSubmit={handleSubmit}
      textareaRef={textareaRef}
    />
  );
}
