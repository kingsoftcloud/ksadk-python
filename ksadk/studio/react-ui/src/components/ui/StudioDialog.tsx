import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";

interface ModalBehaviorProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  closeDisabled?: boolean;
  children: ReactNode;
}

interface ModalLayerRegistration {
  close: () => void;
  disabled: boolean;
}

const modalLayers: ModalLayerRegistration[] = [];
const inertBackground = new Map<HTMLElement, boolean>();
const BACKGROUND_SELECTORS = [
  ".skip-link",
  ".sidebar",
  ".global-header",
  "#mainContent",
  ".page-header",
];

function setStudioBackgroundInert(inert: boolean) {
  if (inert) {
    for (const element of document.querySelectorAll<HTMLElement>(BACKGROUND_SELECTORS.join(","))) {
      if (!inertBackground.has(element)) inertBackground.set(element, element.hasAttribute("inert"));
      element.setAttribute("inert", "");
    }
    return;
  }
  for (const [element, wasInert] of inertBackground) {
    if (element.isConnected && !wasInert) element.removeAttribute("inert");
  }
  inertBackground.clear();
}

function handleModalEscape(event: KeyboardEvent) {
  if (event.key !== "Escape" || !modalLayers.length) return;
  const active = modalLayers[modalLayers.length - 1];
  event.preventDefault();
  event.stopPropagation();
  event.stopImmediatePropagation();
  if (!active.disabled) active.close();
}

function registerModalLayer(registration: ModalLayerRegistration): () => void {
  if (!modalLayers.length) {
    document.addEventListener("keydown", handleModalEscape, true);
    setStudioBackgroundInert(true);
  }
  modalLayers.push(registration);
  return () => {
    const index = modalLayers.lastIndexOf(registration);
    if (index >= 0) modalLayers.splice(index, 1);
    if (!modalLayers.length) {
      document.removeEventListener("keydown", handleModalEscape, true);
      setStudioBackgroundInert(false);
    }
  };
}

function ModalRoot({ open, onOpenChange, closeDisabled = false, children }: ModalBehaviorProps) {
  return (
    <Dialog.Root
      open={open}
      onOpenChange={nextOpen => {
        if (!nextOpen && closeDisabled) return;
        onOpenChange(nextOpen);
      }}
    >
      {children}
    </Dialog.Root>
  );
}

function ModalLayer({
  open,
  className,
  closeDisabled = false,
  role = "dialog",
  onRequestClose,
  children,
}: {
  open: boolean;
  className: string;
  closeDisabled?: boolean;
  role?: "dialog" | "alertdialog";
  onRequestClose: () => void;
  children: ReactNode;
}) {
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const registrationRef = useRef<ModalLayerRegistration>({
    close: onRequestClose,
    disabled: closeDisabled,
  });
  registrationRef.current.close = onRequestClose;
  registrationRef.current.disabled = closeDisabled;

  useEffect(() => {
    if (!open) return undefined;
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    return registerModalLayer(registrationRef.current);
  }, [open]);

  return (
    <Dialog.Portal>
      <div className="overlay">
        <Dialog.Overlay className="overlay-backdrop" />
        <Dialog.Content
          className={className}
          role={role}
          aria-busy={closeDisabled || undefined}
          onEscapeKeyDown={event => {
            event.preventDefault();
            event.stopPropagation();
          }}
          onPointerDownOutside={event => {
            if (closeDisabled) event.preventDefault();
          }}
          onCloseAutoFocus={event => {
            const previousFocus = previousFocusRef.current;
            if (!previousFocus?.isConnected) return;
            event.preventDefault();
            previousFocus.focus();
          }}
        >
          {children}
        </Dialog.Content>
      </div>
    </Dialog.Portal>
  );
}

export function StudioDialog({
  open,
  onOpenChange,
  title,
  description,
  icon,
  footer,
  children,
  closeDisabled = false,
  showClose = true,
  className,
  role = "dialog",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  icon?: ReactNode;
  footer?: ReactNode;
  children?: ReactNode;
  closeDisabled?: boolean;
  showClose?: boolean;
  className?: string;
  role?: "dialog" | "alertdialog";
}) {
  return (
    <ModalRoot open={open} onOpenChange={onOpenChange} closeDisabled={closeDisabled}>
      <ModalLayer
        open={open}
        className={`studio-dialog${className ? ` ${className}` : ""}`}
        closeDisabled={closeDisabled}
        role={role}
        onRequestClose={() => onOpenChange(false)}
      >
        {icon ? <div className="studio-dialog-icon">{icon}</div> : null}
        <header className="studio-dialog-header">
          <div>
            <Dialog.Title>{title}</Dialog.Title>
            {description ? <Dialog.Description>{description}</Dialog.Description> : null}
          </div>
          {showClose ? (
            <Dialog.Close asChild>
              <button className="icon-button tertiary" type="button" aria-label="关闭" disabled={closeDisabled}>
                <X size={16} />
              </button>
            </Dialog.Close>
          ) : null}
        </header>
        {children ? <div className="studio-dialog-body">{children}</div> : null}
        {footer ? <footer className="studio-dialog-footer">{footer}</footer> : null}
      </ModalLayer>
    </ModalRoot>
  );
}

export function StudioDrawer({
  open,
  onOpenChange,
  title,
  subtitle,
  wide = false,
  compact = false,
  closeDisabled = false,
  footer,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  subtitle?: string;
  wide?: boolean;
  compact?: boolean;
  closeDisabled?: boolean;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return (
    <ModalRoot open={open} onOpenChange={onOpenChange} closeDisabled={closeDisabled}>
      <ModalLayer
        open={open}
        className={`drawer${wide ? " wide" : ""}${compact ? " compact" : ""}`}
        closeDisabled={closeDisabled}
        onRequestClose={() => onOpenChange(false)}
      >
        <header className="drawer-header">
          <div>
            <Dialog.Title>{title}</Dialog.Title>
            {subtitle ? <Dialog.Description>{subtitle}</Dialog.Description> : null}
          </div>
          <Dialog.Close asChild>
            <button className="icon-button tertiary" type="button" aria-label="关闭" disabled={closeDisabled}>
              <X size={16} />
            </button>
          </Dialog.Close>
        </header>
        <div className="drawer-body">{children}</div>
        {footer ? <footer className="drawer-footer">{footer}</footer> : null}
      </ModalLayer>
    </ModalRoot>
  );
}
