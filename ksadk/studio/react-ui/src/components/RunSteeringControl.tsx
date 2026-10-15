import { useEffect, useRef, useState } from "react";

interface Props {
  runId: string;
  submit: (runId: string, text: string) => Promise<unknown>;
  onAccepted?: () => void;
}

/** Use the published composer slot while steering remains a host extension. */
export function RunSteeringControl({ runId, submit, onAccepted }: Props) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState("");
  const owner = useRef(0);
  const inFlight = useRef(false);
  useEffect(() => {
    owner.current += 1;
    inFlight.current = false;
    setText(""); setPending(false); setNotice("");
    return () => { owner.current += 1; };
  }, [runId]);
  const send = async () => {
    if (inFlight.current || !text.trim()) return;
    inFlight.current = true;
    const generation = owner.current;
    setPending(true); setNotice("");
    try {
      await submit(runId, text.trim());
      if (owner.current !== generation) return;
      setText(""); setNotice("已接收，将在当前任务中处理。");
      onAccepted?.();
    } catch (error) {
      if (owner.current !== generation) return;
      setNotice(error instanceof Error ? error.message : "纠偏发送失败，请重试。");
    } finally {
      if (owner.current === generation) { inFlight.current = false; setPending(false); }
    }
  };
  return <div className="run-steering-control">
    <label>
      <span>调整当前任务</span>
      <input aria-label="调整当前任务" value={text} maxLength={10000} disabled={pending}
        placeholder="补充约束或纠正当前任务"
        onChange={event => setText(event.target.value)}
        onKeyDown={event => {
          if (event.key === "Enter" && !event.nativeEvent.isComposing) {
            event.preventDefault(); event.stopPropagation(); void send();
          }
        }} />
    </label>
    <button type="button" disabled={pending || !text.trim()} onClick={() => { void send(); }}>
      {pending ? "发送中…" : "发送纠偏"}
    </button>
    {notice ? <span role="status">{notice}</span> : null}
  </div>;
}
