"use client";

/**
 * Modal container (wireframe `.overlay` > `.modal`, max-width 520 px, `.wide` 860 px). Radix
 * Dialog (the shadcn/ui primitive) supplies the behaviour: focus trap, Esc to close, click on the
 * overlay to close, scroll lock, `aria-modal`; the wireframe classes supply the look. The content
 * sits inside the overlay so the stylesheet's flex centring applies unchanged.
 *
 * Content is built from the wireframe's parts: `ModalHead` (`.mhead`), `.mbody`, `.mfoot`.
 */
import { Dialog } from "radix-ui";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function Modal({
  open,
  onOpenChange,
  wide = false,
  label,
  className,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  wide?: boolean;
  /** An extra class on `.modal` (e.g. the source viewer's full-screen phone layout). */
  className?: string;
  /** Accessible name when the content has no `ModalHead` title. */
  label: string;
  children: ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="overlay on" id="overlay">
          <Dialog.Content className={cn("modal", wide && "wide", className)} aria-describedby={undefined}>
            <Dialog.Title className="sr-only">{label}</Dialog.Title>
            {children}
          </Dialog.Content>
        </Dialog.Overlay>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** Modal header (wireframe `.mhead`): icon tile, eyebrow, title, lead text and the ✕ button. */
export function ModalHead({
  icon,
  iconStyle,
  eyebrow,
  eyebrowColor = "var(--brand)",
  title,
  lead,
}: {
  icon?: ReactNode;
  iconStyle?: React.CSSProperties;
  eyebrow?: ReactNode;
  eyebrowColor?: string;
  title: ReactNode;
  lead?: ReactNode;
}) {
  return (
    <div className="mhead">
      {icon && (
        <div className="mi" style={iconStyle}>
          {icon}
        </div>
      )}
      <div className="mt">
        {eyebrow && (
          <div className="meyebrow" style={{ color: eyebrowColor }}>
            {eyebrow}
          </div>
        )}
        <h2>{title}</h2>
        {lead && <p>{lead}</p>}
      </div>
      <Dialog.Close className="x" aria-label="Close">
        ✕
      </Dialog.Close>
    </div>
  );
}

export const ModalClose = Dialog.Close;
