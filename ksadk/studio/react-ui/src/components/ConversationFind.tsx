import { useEffect, useRef, useState } from "react";
import { Search, X } from "lucide-react";
import type { HistorySearchResult } from "@kingsoftcloud/ksadk-web/conversation";

type Props = {
  search: (query: string, signal: AbortSignal, onProgress: (result: HistorySearchResult) => void) => Promise<HistorySearchResult>;
  onReveal: (messageId: string) => void;
  onClose: () => void;
};
const EMPTY: HistorySearchResult = { matches: [], matchedMessages: 0, searchedMessages: 0, complete: false };

/** An explicit history query; its lifetime is independent of the Composer. */
export function ConversationFind({ search, onReveal, onClose }: Props) {
  const [query, setQuery] = useState("");
  const [revision, setRevision] = useState(0);
  const [result, setResult] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cancelled, setCancelled] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => {
    const abort = new AbortController();
    abortRef.current = abort;
    setResult(EMPTY); setError(""); setCancelled(false);
    setBusy(Boolean(query.trim()));
    if (!query.trim()) return () => abort.abort();
    const timer = window.setTimeout(() => {
      void search(query, abort.signal, next => { if (!abort.signal.aborted) setResult(next); })
        .then(next => { if (!abort.signal.aborted) setResult(next); })
        .catch(reason => {
          if (!abort.signal.aborted) setError(reason instanceof Error ? reason.message : "查找失败，请重试。");
        })
        .finally(() => { if (!abort.signal.aborted) setBusy(false); });
    }, 250);
    return () => { window.clearTimeout(timer); abort.abort(); };
  }, [query, revision, search]);

  return <section className="conversation-find" aria-label="查找当前会话" onKeyDown={event => {
    if (event.key === "Escape") { event.preventDefault(); onClose(); }
  }}>
    <div className="conversation-find-controls">
      <Search size={16} aria-hidden="true" />
      <input ref={inputRef} type="search" aria-label="查找当前会话正文" placeholder="查找当前会话正文…"
        value={query} onChange={event => setQuery(event.target.value)} />
      {busy && <button className="button tertiary" type="button" onClick={() => {
        abortRef.current?.abort(); setBusy(false); setCancelled(true);
      }}>停止查找</button>}
      <button className="icon-button tertiary" type="button" aria-label="关闭会话查找" onClick={onClose}><X size={16} /></button>
    </div>
    {query.trim() && <>
      <p role="status" className="conversation-find-status">
        {busy ? `正在查找更早历史，已检查 ${result.searchedMessages} 条消息…`
          : result.complete ? `已查找全部历史，${result.matchedMessages} 条消息匹配`
            : `已查找 ${result.searchedMessages} 条消息，${result.matchedMessages} 条匹配${cancelled ? "；查找已停止" : ""}`}
      </p>
      {error && <p role="alert">{error}</p>}
      {(error || cancelled) && <button className="button secondary" type="button" onClick={() => setRevision(value => value + 1)}>重新查找</button>}
      <ol className="conversation-find-results" aria-label="正文查找结果">
        {result.matches.map(match => <li key={match.messageId}>
          <button type="button" data-search-message-id={match.messageId} onClick={() => {
            // Stop pagination before revealing a row, so a later prepend
            // cannot immediately move the reader away from the chosen result.
            if (busy) { abortRef.current?.abort(); setBusy(false); setCancelled(true); }
            onReveal(match.messageId);
          }}>
            <span>{match.role === "user" ? "你" : match.role === "model" ? "Agent" : "消息"}</span>
            <span>{match.excerpt}</span>
          </button>
        </li>)}
      </ol>
      {result.matchedMessages > result.matches.length && <p>展示前 {result.matches.length} 条匹配消息，请缩小关键词范围。</p>}
    </>}
    {!query.trim() && <p className="conversation-find-status">搜索正文、工具结果和附件名称，也会读取尚未显示的历史消息。</p>}
  </section>;
}
