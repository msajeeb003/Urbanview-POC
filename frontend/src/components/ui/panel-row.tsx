import type { CSSProperties, ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * One label / value line of a panel section (wireframe `.prow`): label left in ink-2, value right
 * in JetBrains Mono 700, optional unit in a lighter span, dashed divider below.
 *
 * - `text`: long textual values (land use, document names) use the wireframe's inline 11.5 px and
 *   may wrap (the mock's `.pv` never wraps: its strings were short; real ones are not), so a value
 *   that fits looks exactly as in the mock and a longer one breaks instead of being cut off.
 * - `accent`: brand-dark value (the computed max GFA row).
 * - `after`: something placed after the value, e.g. a `SourceRef` chip for that value.
 */
export function PanelRow({
  label,
  value,
  unit,
  text = false,
  accent = false,
  after,
  className,
  style,
}: {
  label: ReactNode;
  value: ReactNode;
  unit?: ReactNode;
  text?: boolean;
  accent?: boolean;
  after?: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  const valueStyle: CSSProperties = {
    ...(text ? { fontSize: "11.5px", whiteSpace: "normal", overflowWrap: "anywhere" } : {}),
    ...(accent ? { color: "var(--brand-dark)" } : {}),
  };
  return (
    <div className={cn("prow", className)} style={style}>
      <span className="pk">{label}</span>
      <span className="pv" style={Object.keys(valueStyle).length ? valueStyle : undefined}>
        {value}
        {unit != null && <span className="u">{unit}</span>}
        {after}
      </span>
    </div>
  );
}
