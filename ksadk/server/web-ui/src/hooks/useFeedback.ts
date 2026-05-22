import { useCallback } from 'react';
import { useMessageStore } from '../stores/message.js';
import {
  applyOptimisticFeedback,
  buildGetFeedbackPayload,
  buildUpsertFeedbackPayload,
  clearFeedback,
  markFeedbackSaved,
  normalizeFeedback,
  rollbackFeedback,
} from '../utils/feedback.js';
import type { Message } from '../components/chat/types.js';
import type { ApiFacade } from '../core/api/types.js';

type UseFeedbackContext = {
  agentId: string;
  currentSessionId: string | null;
  isStreaming: boolean;
  api: ApiFacade;
  submitDraft: (
    text: string,
    attachments: File[],
    responsesInput?: unknown,
    previousResponseId?: string,
  ) => Promise<void>;
};

export function useFeedback(ctx: UseFeedbackContext) {
  const {
    agentId,
    currentSessionId,
    isStreaming,
    api,
    submitDraft,
  } = ctx;

  const submitResponseFeedback = useCallback(
    async (options: {
      message: Message;
      rating: 'up' | 'down';
      comment?: string;
    }) => {
      if (!currentSessionId) {
        return;
      }

      let previousFeedback: NonNullable<Message['feedback']> | null = null;
      useMessageStore.getState().patchMessages((prev) => {
        const result = applyOptimisticFeedback(prev, {
          messageId: options.message.id,
          rating: options.rating,
          comment: options.comment || '',
        });
        previousFeedback = result.previousFeedback;
        return result.nextMessages;
      });

      try {
        const data = await api.upsertResponseFeedback(
          buildUpsertFeedbackPayload({
            agentId,
            sessionId: currentSessionId,
            message: options.message,
            rating: options.rating,
            comment: options.comment || '',
          }),
        );
        const feedback = normalizeFeedback((data as Record<string, unknown>)?.Feedback);
        if (feedback) {
          useMessageStore.getState().patchMessages((prev) =>
            markFeedbackSaved(prev, {
              messageId: options.message.id,
              feedback,
            }),
          );
        }
      } catch (error) {
        console.error('Failed to submit response feedback:', error);
        useMessageStore.getState().patchMessages((prev) =>
          rollbackFeedback(prev, {
            messageId: options.message.id,
            previousFeedback,
          }),
        );
      }
    },
    [agentId, currentSessionId, api],
  );

  const deleteResponseFeedback = useCallback(
    async (message: Message) => {
      if (!currentSessionId) {
        return;
      }

      const previousFeedback: NonNullable<Message['feedback']> | null = message.feedback
        ? { ...message.feedback }
        : null;
      useMessageStore.getState().patchMessages((prev) => clearFeedback(prev, { messageId: message.id }));

      try {
        await api.deleteResponseFeedback(
          buildGetFeedbackPayload({
            agentId,
            sessionId: currentSessionId,
            message,
          }),
        );
      } catch (error) {
        console.error('Failed to delete response feedback:', error);
        useMessageStore.getState().patchMessages((prev) =>
          rollbackFeedback(prev, {
            messageId: message.id,
            previousFeedback,
          }),
        );
      }
    },
    [agentId, currentSessionId, api],
  );

  const respondToApproval = useCallback(
    (options: {
      approvalRequestId: string;
      approve: boolean;
      previousResponseId?: string;
    }) => {
      if (!options.approvalRequestId || isStreaming) return;
      useMessageStore.getState().patchMessages((prev) =>
        prev.map((message) => ({
          ...message,
          tools: message.tools
            ? Object.fromEntries(
                Object.entries(message.tools).map(([name, tool]) => [
                  name,
                  tool.approvalRequestId === options.approvalRequestId
                    ? {
                        ...tool,
                        approvalStatus: options.approve ? 'approved' : 'rejected',
                      }
                    : tool,
                ]),
              )
            : message.tools,
        })),
      );
      useMessageStore.getState().patchMessages((prev) => [
        ...prev,
        {
          id: String(Date.now() + Math.random()),
          role: 'system',
          content: options.approve ? '已批准工具调用，正在继续运行。' : '已拒绝工具调用，正在通知运行时。',
          timestamp: Date.now(),
        },
      ]);
      void submitDraft(
        '',
        [],
        [
          {
            type: 'mcp_approval_response',
            approval_request_id: options.approvalRequestId,
            approve: options.approve,
          },
        ],
        options.previousResponseId,
      );
    },
    [isStreaming, submitDraft],
  );

  return { submitResponseFeedback, deleteResponseFeedback, respondToApproval };
}
