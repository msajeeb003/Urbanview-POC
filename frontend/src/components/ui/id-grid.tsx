import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export interface IdCell {
  key: string;
  label: ReactNode;
  value: ReactNode;
  /** Spans both columns (wireframe `.idcell.full`, e.g. the governing document). */
  full?: boolean;
  /** Smaller value text for long names (the wireframe's inline 11 px on the governing document). */
  small?: boolean;
}

/** Two-column bordered grid of key / value cells at the top of a parcel panel (`.idgrid`). */
export function IdGrid({ cells, className }: { cells: IdCell[]; className?: string }) {
  return (
    <div className={cn("idgrid", className)}>
      {cells.map((c) => (
        <div key={c.key} className={cn("idcell", c.full && "full")}>
          <div className="k">{c.label}</div>
          <div className="v" style={c.small ? { fontSize: "11px" } : undefined}>
            {c.value}
          </div>
        </div>
      ))}
    </div>
  );
}
