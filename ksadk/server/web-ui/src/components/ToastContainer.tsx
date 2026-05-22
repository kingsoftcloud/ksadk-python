import { useEffect } from 'react';
import { useUIStore } from '../stores/ui.js';
import { X } from 'lucide-react';

export function ToastContainer() {
  const toasts = useUIStore(s => s.toasts);
  const dismissToast = useUIStore(s => s.dismissToast);

  useEffect(() => {
    if (!toasts.length) return;
    const oldest = toasts[0];
    const remaining = 3000 - (Date.now() - oldest.createdAt);
    if (remaining <= 0) {
      dismissToast(oldest.id);
      return;
    }
    const timer = setTimeout(() => dismissToast(oldest.id), remaining);
    return () => clearTimeout(timer);
  }, [toasts, dismissToast]);

  if (!toasts.length) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`flex items-center gap-2 rounded-lg px-4 py-3 text-sm shadow-lg transition-all ${
            toast.variant === 'error'
              ? 'bg-red-600 text-white'
              : 'bg-slate-800 text-white dark:bg-slate-200 dark:text-slate-900'
          }`}
        >
          <span className="flex-1">{toast.message}</span>
          <button
            onClick={() => dismissToast(toast.id)}
            className="shrink-0 rounded p-0.5 opacity-70 hover:opacity-100"
          >
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}
