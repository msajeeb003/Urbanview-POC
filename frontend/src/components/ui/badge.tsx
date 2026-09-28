import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** Tier badge next to a section label (wireframe `.badge.free`: brand tint / brand-dark). */
export function Badge({
  tone,
  children,
  className,
}: {
  tone: "free";
  children?: ReactNode;
  className?: string;
}) {
  return <span className={cn("badge", tone, className)}>{children ?? "Free"}</span>;
}
