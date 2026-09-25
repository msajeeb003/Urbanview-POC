import type { MouseEventHandler } from "react";

import { cn } from "@/lib/utils";

import { IconDocSmall } from "./icons";

/**
 * The "source" chip (wireframe `.srcref`): document glyph + "source", brand on brand tint, pill.
 * With `onClick` it is a button (opens the cited page through `viewer_url`, and the caller emits
 * `source_reference_opened`); without, a plain marker.
 */
export function SourceRef({
  onClick,
  label = "source",
  title = "Traceable to source document",
  className,
}: {
  onClick?: MouseEventHandler<HTMLButtonElement>;
  label?: string;
  title?: string;
  className?: string;
}) {
  const content = (
    <>
      <IconDocSmall />
      {label}
    </>
  );
  if (onClick) {
    return (
      <button type="button" className={cn("srcref", className)} title={title} onClick={onClick}>
        {content}
      </button>
    );
  }
  return (
    <span className={cn("srcref", className)} title={title}>
      {content}
    </span>
  );
}
