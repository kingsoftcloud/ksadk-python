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
  const submitResponseFeedback = useCallback(
    async (options: {
      message: Message;
      rating: 'up' | 'down';
      comment?: string;
    }) => {
      if (!ctx.currentSessionId) {
        return;
      }

      let previousFeedback: Message['feedback'] | null = null;
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
        const data = await ctx.api.upsertResponseFeedback(
          buildUpsertFeedbackPayload({
            agentId: ctx.agentId,
            sessionId: ctx.currentSessionId!,
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
    [ctx.agentId, ctx.currentSessionId, ctx.api],
  );

  const deleteResponseFeedback = useCallback(
    async (message: Message) => {
      if (!ctx.currentSessionId) {
        return;
      }

      const previousFeedback = message.feedback ? { ...message.feedback } : null;
      useMessageStore.getState().patchMessages((prev) => clearFeedback(prev, { messageId: message.id }));

      try {
        await ctx.api.deleteResponseFeedback(
          buildGetFeedbackPayload({
            agentId: ctx.agentId,
            sessionId: ctx.currentSessionId!,
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
    [ctx.agentId, ctx.currentSessionId, ctx.api],
  );

  const respondToApproval = useCallback(
    (options: {
      approvalRequestId: string;
      approve: boolean;
      previousResponseId?: string;
    }) => {
      if (!options.approvalRequestId || ctx.isStreaming) return;
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
      void ctx.submitDraft(
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
    [ctx.isStreaming, ctx.submitDraft],
  );

  return { submitResponseFeedback, deleteResponseFeedback, respondToApproval };
}
