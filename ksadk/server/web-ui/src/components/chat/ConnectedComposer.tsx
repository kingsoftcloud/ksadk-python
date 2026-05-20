import React, { useEffect, useRef, useCallback } from 'react';
import { useUIStore } from '../../stores/ui.js';
import { useStreamingStore } from '../../stores/streaming.js';
import { ChatComposer } from './ChatComposer';
import { mergeAttachmentFiles, extractClipboardFiles } from '../../utils/attachment.js';

type ConnectedComposerProps = {
  composerMaxHeight: number;
  submitDraft: (text: string, attachments: File[]) => Promise<void>;
  stopGeneration: () => void;
  isMobile: boolean;
};

export function ConnectedComposer({
  composerMaxHeight,
  submitDraft,
  stopGeneration,
  isMobile,
}: ConnectedComposerProps) {
  const input = useUIStore(s => s.input);
  const attachments = useUIStore(s => s.attachments);
  const isStreaming = useStreamingStore(s => s.isStreaming);
  const queuedDrafts = useUIStore(s => s.queuedDrafts);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

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
      composerContextIndicator={null}
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
      onSubmit={handleSubmit}
      textareaRef={textareaRef}
    />
  );
}
