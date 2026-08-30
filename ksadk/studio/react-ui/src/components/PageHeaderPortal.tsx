import { useLayoutEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

function HeaderPortal({ targetId, children }: { targetId: string; children: ReactNode }) {
  const [target, setTarget] = useState<HTMLElement | null>(null);

  useLayoutEffect(() => {
    setTarget(document.getElementById(targetId));
  }, [targetId]);

  // The inline fallback keeps isolated page tests meaningful while the App
  // owns the real global header target.
  return target ? createPortal(children, target) : <>{children}</>;
}

export function PageHeaderTools({ children }: { children: ReactNode }) {
  return <HeaderPortal targetId="pageHeaderTools">{children}</HeaderPortal>;
}

export function PageHeaderActions({ children }: { children: ReactNode }) {
  return <HeaderPortal targetId="pageHeaderActions">{children}</HeaderPortal>;
}
