import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import { IconLockSmall } from "./icons";

/**
 * Tier badge next to a section label (wireframe `.badge`): `free` = brand tint / brand-dark,
 * `paid` = gold tint / gold with a padlock. Default labels are the wireframe's.
 */
export function Badge({
  tone,
  children,
  className,
}: {
  tone: "free" | "paid";
  children?: ReactNode;
  className?: string;
}) {
  return (
    <span className={cn("badge", tone, className)}>
      {tone === "paid" && <IconLockSmall style={{ marginTop: -1 }} />}
      {children ?? (tone === "free" ? "Free" : "Subscription")}
    </span>
  );
}
