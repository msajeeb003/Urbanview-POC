import type { ButtonHTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/utils";

export type CtaVariant = "primary" | "gold" | "ghost" | "line";

/**
 * Panel / modal call-to-action (wireframe `.cta`): 50 px, left-aligned icon + label, optional
 * trailing mono text (`trailing`, e.g. the price). Variants: `primary` brand, `gold` paid,
 * `ghost` white with hairline, `line` white with hairline and ink-2 text. Inside `.mfoot` the
 * stylesheet turns them into 44 px auto-width buttons.
 */
export function Cta({
  variant,
  icon,
  trailing,
  children,
  className,
  type = "button",
  ...rest
}: {
  variant: CtaVariant;
  icon?: ReactNode;
  trailing?: ReactNode;
  children: ReactNode;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button type={type} className={cn("cta", variant, className)} {...rest}>
      {icon}
      {children}
      {trailing != null && <small>{trailing}</small>}
    </button>
  );
}
