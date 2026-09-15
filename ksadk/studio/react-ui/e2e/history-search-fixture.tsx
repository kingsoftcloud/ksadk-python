import { createRoot } from 'react-dom/client';
import { useRef, useState } from 'react';
import { ChatMessageList, type Message } from '@kingsoftcloud/ksadk-web/chat/timeline';
import { scanConversationHistory } from '@kingsoftcloud/ksadk-web/conversation';
import { ConversationFind } from '../src/components/ConversationFind';
import { CompactHarnessTimeline } from '../src/components/CompactHarnessTimeline';
import '@kingsoftcloud/ksadk-web/styles';
import '../src/index.css';
import '../src/kingdesign.css';
import '../src/studio-refinement.css';
import '../src/layout-simplification.css';

// Deterministic synthetic projection; no model, host activation, cloud account
// or execution command is involved in this renderer/history-query gate.
const history: Message[] = Array.from({ length: 2000 }, (_, index) => ({
  id: `message-${index}`, role: index % 4 === 0 ? 'user' : 'model',
  content: [802, 806, 810].includes(index) ? `验收目标位于更早的历史：请定位到这条消息 ${index}。` : `第 ${index} 条消息：这是可搜索的中文历史正文。`,
  timestamp: index, runId: `fixture-${Math.floor(index / 4)}`, status: 'completed',
}));
const noop = () => {};

function Fixture() {
  const compact = new URLSearchParams(location.search).get('mode') === 'compact';
  const scrollRef = useRef<HTMLDivElement>(null);
  const [messages, setMessages] = useState(history.slice(-50));
  const loaded = useRef(messages);
  const offset = useRef(1950);
  const [reveal, setReveal] = useState<{ id: string; request: number } | null>(null);
  const search = useRef((query: string, signal: AbortSignal, onProgress: Parameters<typeof scanConversationHistory>[0]['onProgress']) =>
    scanConversationHistory({ query, signal, onProgress,
      snapshot: () => ({ owner: 'synthetic/session', messages: loaded.current, hasMore: offset.current > 0,
        checkpoint: String(offset.current), loading: false }),
      readOlder: async () => {
        signal.throwIfAborted();
        offset.current = Math.max(0, offset.current - 100);
        loaded.current = history.slice(offset.current);
        setMessages(loaded.current);
      },
    })).current;
  const props = { messages, agentName: '回放 Agent', isMobile: false, isStreaming: false, activity: null,
    onDeleteFeedback: noop, onSubmitFeedback: noop, onRespondToApproval: noop,
    revealMessage: reveal, className: 'studio-chat-timeline' };
  return <main className="ksadk-web studio-chat-shell" data-integrated-header="true" style={{ height: '100vh', display: 'block', maxWidth: 1000, margin: 'auto' }}>
    <section className="chat-conversation" style={{ height: '100%' }}>
    <ConversationFind search={search} onClose={noop} onReveal={id => setReveal(value => ({ id, request: (value?.request || 0) + 1 }))} />
    {compact ? <CompactHarnessTimeline {...props} sessionId="fixture-session" />
      : <ChatMessageList {...props} contextIndicator={null} scrollRef={scrollRef} onOpenAttachmentPreview={noop} />}
    <textarea aria-label="下一条消息" placeholder="搜索和跳转时保留此输入" style={{ flexShrink: 0, minHeight: 80 }} />
    </section>
  </main>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
