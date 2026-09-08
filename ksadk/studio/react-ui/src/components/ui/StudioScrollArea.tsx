import * as ScrollArea from "@radix-ui/react-scroll-area";
import type { ReactNode } from "react";

export function StudioScrollArea({
  children,
  className,
  viewportClassName,
}: {
  children: ReactNode;
  className?: string;
  viewportClassName?: string;
}) {
  return (
    <ScrollArea.Root className={`studio-scroll-area${className ? ` ${className}` : ""}`}>
      <ScrollArea.Viewport className={`studio-scroll-viewport${viewportClassName ? ` ${viewportClassName}` : ""}`}>
        {children}
      </ScrollArea.Viewport>
      <ScrollArea.Scrollbar className="studio-scrollbar" orientation="vertical">
        <ScrollArea.Thumb className="studio-scroll-thumb" />
      </ScrollArea.Scrollbar>
      <ScrollArea.Scrollbar className="studio-scrollbar horizontal" orientation="horizontal">
        <ScrollArea.Thumb className="studio-scroll-thumb" />
      </ScrollArea.Scrollbar>
      <ScrollArea.Corner className="studio-scroll-corner" />
    </ScrollArea.Root>
  );
}
