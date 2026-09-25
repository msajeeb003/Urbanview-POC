import type { CSSProperties } from "react";

import type { Swatch } from "@/lib/layers";
import { cn } from "@/lib/utils";

import { IconCheckTiny, IconLockSmall } from "./icons";

/** Inline style of the 24 px swatch tile, per the wireframe's `swatchHTML`. */
export function swatchStyle(sw: Swatch): CSSProperties {
  switch (sw.kind) {
    case "zones":
      return { background: "conic-gradient(#B5744A,#BE9A44,#8A7A8E,#5E8A82,#7C8A4F)" };
    case "dash":
      return { background: "#fff", border: "1.5px dashed #B5613B" };
    case "docdash":
      return { background: "#fff", border: "2px dashed #B3A894" };
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
  /** Core layer: always on, muted check, not toggleable (◆ marker kept in the markup). */
  core?: boolean;
  /** A required layer is off: dimmed card with a gold ▲. */
  dependencyMissing?: boolean;
  /** Paid layer without entitlement: gold padlock and the "Subscription" sub-label. */
  paidLocked?: boolean;
  /** Optional mono sub-label under the name (ignored while paid-locked). */
  sub?: string;
  /** The "not covered" note: the published data has nothing for this layer yet (muted sub-label). */
  note?: string;
  /** "Subscription" on a paid-locked card (the shell's language). */
  lockedLabel?: string;
  title?: string;
  onClick?: () => void;
}

/** One card of the layer rail (wireframe `.lyr`), markup identical to the mock's template. */
export function LayerCard({
  name,
  swatch,
  on,
  core = false,
  dependencyMissing = false,
  paidLocked = false,
  sub,
  note,
  lockedLabel = "Subscription",
  title,
  onClick,
}: LayerCardProps) {
  return (
    <button
      type="button"
      className={cn(
        "lyr",
        on && !paidLocked && "on",
        core && "core",
        dependencyMissing && "dep",
        paidLocked && "paidlayer",
      )}
      title={title}
      aria-pressed={core ? undefined : on && !paidLocked}
      aria-disabled={core || undefined}
      onClick={onClick}
    >
      <div className="swatch" style={swatchStyle(swatch)} />
      <span className="lyrtext">
        <span className="nm">{name}</span>
        {paidLocked ? (
          <span className="subnm lockedsub">{lockedLabel}</span>
        ) : sub ? (
          <span className="subnm">{sub}</span>
        ) : note ? (
          <span className="subnm nodatasub">{note}</span>
        ) : null}
      </span>
      <span className="chk">
        <IconCheckTiny />
      </span>
      {core && <span className="lock">◆</span>}
      {dependencyMissing && <span className="depwarn">▲</span>}
      {paidLocked && (
        <span className="paidlock">
          <IconLockSmall />
        </span>
      )}
    </button>
  );
}

/** The note under a dependent layer that is on while its requirement is off (`.depnote`). */
export function DependencyNote({
  requiredName,
  onClick,
  words = { needs: "Needs", tap: "tap to turn on", title: `Turn on ${requiredName}` },
}: {
  requiredName: string;
  onClick: () => void;
  /** The note's words in the shell's language (the mock's English by default). */
  words?: { needs: string; tap: string; title: string };
}) {
  return (
    <button type="button" className="depnote" title={words.title} onClick={onClick}>
      {words.needs}
      <b>{requiredName}</b>
      {words.tap}
    </button>
  );
}
