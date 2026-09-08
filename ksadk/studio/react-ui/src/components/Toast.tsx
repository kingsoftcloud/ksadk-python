import { useEffect, useState } from "react";
import { Check, CircleAlert } from "lucide-react";

interface ToastItem { key: number; toastKey: string; title: string; message: string; type: "success" | "error" }

type Listener = (items: ToastItem[]) => void;

let items: ToastItem[] = [];
let seq = 0;
const listeners = new Set<Listener>();

function emit() {
  listeners.forEach(l => l([...items]));
}

/** Toast 去重，成功 3.6s / 失败 7s 自动消失。 */
export function showToast(title: string, message = "", type: "success" | "error" = "success") {
  const toastKey = `${type}:${title}:${message}`;
  if (items.some(t => t.toastKey === toastKey)) return;
  const item: ToastItem = { key: ++seq, toastKey, title, message, type };
  items = [...items, item];
  emit();
  window.setTimeout(() => {
    items = items.filter(t => t.key !== item.key);
    emit();
  }, type === "error" ? 7000 : 3600);
}

export function ToastRegion() {
  const [list, setList] = useState<ToastItem[]>(items);
  useEffect(() => {
    const l: Listener = next => setList(next);
    listeners.add(l);
    return () => { listeners.delete(l); };
  }, []);
  return (
    <div className="toast-region" aria-live="polite" aria-atomic="true">
      {list.map(t => (
        <div key={t.key} className={`toast ${t.type}`}>
          {t.type === "error" ? <CircleAlert size={15} /> : <Check size={15} />}
          <div><strong>{t.title}</strong>{t.message ? <p>{t.message}</p> : null}</div>
        </div>
      ))}
    </div>
  );
}
