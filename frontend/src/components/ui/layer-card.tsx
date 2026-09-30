import type { CSSProperties } from "react";

import type { Swatch } from "@/lib/layers";
import { cn } from "@/lib/utils";

import { IconCheckTiny } from "./icons";

/** Inline style of the 24 px swatch tile, per the wireframe's `swatchHTML`. */
export function swatchStyle(sw: Swatch): CSSProperties {
  switch (sw.kind) {
    case "zones":
      return { background: "conic-gradient(#B5744A,#BE9A44,#8A7A8E,#5E8A82,#7C8A4F)" };
    case "dash":
      return { background: "#fff", border: "1.5px dashed #B5613B" };
    case "blockdash":
      return { background: "#fff", border: "1.5px dotted #B3A894" };
    case "heat1":
      return { background: "linear-gradient(135deg,#EFE3CE,#B4744A)" };
    case "heat2":
      return { background: "linear-gradient(135deg,#F1E7CF,#B5853F)" };
    case "color":
      return { background: sw.color };
  }
}

export interface LayerCardProps {
  name: string;
  swatch: Swatch;
  /** Visible (brand check, brand-dark name). */
  on: boolean;
  /** Optional mono sub-label under the name. */
  sub?: string;
  /**
   * Why the map draws nothing for the layer right now (muted sub-label, under `sub` when both):
   * `no_data` = the published data has nothing for it yet, `zoom_in` = it is drawn closer in.
   */
  note?: string;
  noteKind?: "no_data" | "zoom_in";
  title?: string;
  onClick?: () => void;
}

/** One card of the layer rail (wireframe `.lyr`), markup identical to the mock's template. */
export function LayerCard({
  name,
  swatch,
  on,
  sub,
  note,
  noteKind = "no_data",
  title,
  onClick,
}: LayerCardProps) {
  return (
    <button
      type="button"
      className={cn("lyr", on && "on")}
      title={title}
      aria-pressed={on}
      onClick={onClick}
    >
      <div className="swatch" style={swatchStyle(swatch)} />
      <span className="lyrtext">
        <span className="nm">{name}</span>
        {sub && <span className="subnm">{sub}</span>}
        {note && <span className={noteKind === "zoom_in" ? "subnm zoomsub" : "subnm nodatasub"}>{note}</span>}
      </span>
      <span className="chk">
        <IconCheckTiny />
      </span>
    </button>
  );
}
