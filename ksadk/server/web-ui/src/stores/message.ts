import { create } from 'zustand';
import type { Message } from '../components/chat/types.js';

type MessageState = {
  messages: Message[];
};

type MessageActions = {
  setMessages: (messages: Message[]) => void;
  patchMessages: (updater: (prev: Message[]) => Message[]) => void;
};

export const useMessageStore = create<MessageState & MessageActions>()((set) => ({
  messages: [],
  setMessages: (messages) => set({ messages }),
  patchMessages: (updater) => set((s) => ({ messages: updater(s.messages) })),
}));